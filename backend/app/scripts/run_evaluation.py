"""端到端评测脚本。

对黄金评测集中的每道题调用 Agent，将生成的 SQL 与参考 SQL 分别执行，
比对结果集是否一致，据此计算执行准确率（Execution Accuracy）。

用法：
    uv run python -m app.scripts.run_evaluation -c evaluation/golden_set.yaml
    uv run python -m app.scripts.run_evaluation -c evaluation/golden_set.yaml --level L1 L2
    uv run python -m app.scripts.run_evaluation -c evaluation/golden_set.yaml --concurrency 4

报告输出至 evaluation/reports/ 目录，同时生成 JSON（完整明细）与
Markdown（可直接粘贴进 README）两份。
"""

import argparse
import asyncio
import json
import re
import statistics
import time
from datetime import date, datetime
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any, Optional

import yaml

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

PROJECT_ROOT = Path(__file__).parents[2]

_STRING_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")

TABLES: set[str] = set()


def warehouse_tables() -> set[str]:
    """从元数据配置中读取数仓表名，用于判断 SQL 是否真的触碰了业务数据。"""
    meta = yaml.safe_load(
        (PROJECT_ROOT / "conf" / "meta_config.yaml").read_text(encoding="utf-8")
    )
    return {t["name"].lower() for t in meta.get("tables", [])}


def is_refusal(sql: Optional[str], rows: Optional[list[dict]], tables: set[str]) -> bool:
    """判断 Agent 是否实质上拒绝作答。

    面对数仓里不存在的字段或指标，模型通常不会抛异常，而是返回一句说明
    （`SELECT '无法计算…' AS msg`）或一个空集（`SELECT NULL … LIMIT 0`）。
    这类输出没有碰任何业务表，不会把错误数字伪装成答案，应判为正确拒答。

    真正危险的是引用了真实表、也确实返回了数据的情况——比如把 region_name
    起别名叫「门店」——用户拿到的是一张张冠李戴的结果表。这种必须判错。
    """
    if not sql:
        return True
    # 先剥掉字符串字面量，避免拒答说明文案里出现表名造成误判
    stripped = _STRING_LITERAL.sub("", sql.lower())
    if not any(t in stripped for t in tables):
        return True
    return not rows


# --------------------------------------------------------------- 结果集比对


def _normalize_value(value: Any) -> Optional[str]:
    """把单元格归一化成可比较的字符串。

    数值统一四舍五入到 4 位小数后再转字符串，避免 Decimal(100) 与 float(100.0)
    这类等价但不相等的情况被误判为不一致。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, Decimal)):
        return f"{round(float(value), 4):.4f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value).strip()


def _project(rows: list[dict], indices: tuple[int, ...], ordered: bool) -> list:
    """把结果集投影到指定列上，归一化成可比较的结构。

    每行内部按值的字符串排序，使比对与 SELECT 的列顺序无关
    （同一个问题写成 `SELECT a, b` 和 `SELECT b, a` 应判为等价）。
    参考 SQL 含 ORDER BY 时保留行序，否则按行排序后比对。
    """
    projected = [
        tuple(sorted(
            (_normalize_value(list(row.values())[i]) or "\x00") for i in indices
        ))
        for row in rows
    ]
    return projected if ordered else sorted(projected)


def compare_result_sets(
    actual: list[dict], expected: list[dict], reference_sql: str
) -> bool:
    """比对结果集是否等价。

    参考 SQL 给出的是「最小正确答案」，因此允许 Agent 多返回列：
    问「最畅销的品类是什么」时，只返回品类名与同时返回品类名和销量
    都算答对，用户想要的信息都拿到了。反之列数少于参考答案则判错——
    信息缺失就是没答对。多列时会枚举列的组合，命中任一组即通过，
    以此在容忍额外列的同时保留行内各列的对应关系。
    """
    ordered = "order by" in reference_sql.lower()
    if len(actual) != len(expected):
        return False
    if not expected:
        return not actual

    n_expected = len(expected[0])
    n_actual = len(actual[0]) if actual else 0
    if n_actual < n_expected:
        return False

    target = _project(expected, tuple(range(n_expected)), ordered)
    for combo in combinations(range(n_actual), n_expected):
        if _project(actual, combo, ordered) == target:
            return True
    return False


# ------------------------------------------------------------------- 单题评测


async def evaluate_one(case: dict, semaphore: asyncio.Semaphore) -> dict:
    """评测单道题，返回该题的完整明细。"""
    async with semaphore:
        record: dict[str, Any] = {
            "id": case["id"],
            "level": case["level"],
            "question": case["question"],
            "expect_failure": bool(case.get("expect_failure", False)),
            "generated_sql": None,
            "passed": False,
            "reason": "",
            "latency_ms": None,
        }

        started = time.perf_counter()
        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            context = DataAgentContext(
                embedding_client=embedding_client_manager.client,
                column_qdrant_repository=ColumnQdrantRepository(
                    qdrant_client_manager.client
                ),
                value_es_repository=ValueESRepository(es_client_manager.client),
                metric_qdrant_repository=MetricQdrantRepository(
                    qdrant_client_manager.client
                ),
                meta_mysql_repository=MetaMySQLRepository(meta_session),
                dw_mysql_repository=DWMySQLRepository(dw_session),
            )

            agent_error: Optional[str] = None
            final_state: Optional[dict] = None
            try:
                final_state = await graph.ainvoke(
                    input=DataAgentState(query=case["question"]), context=context
                )
            except Exception as exc:  # Agent 链路任一环节失败
                agent_error = f"{type(exc).__name__}: {exc}"

            record["latency_ms"] = round((time.perf_counter() - started) * 1000)
            if final_state:
                record["generated_sql"] = final_state.get("sql")

            # --- 拒答类题目：期望 Agent 不要编造答案 ---
            if record["expect_failure"]:
                if agent_error:
                    record["passed"] = True
                    record["reason"] = f"链路报错，未产出答案({agent_error[:100]})"
                    return record
                rows = None
                if record["generated_sql"]:
                    try:
                        rows = await DWMySQLRepository(dw_session).execute_sql(
                            record["generated_sql"]
                        )
                    except Exception:
                        record["passed"] = True
                        record["reason"] = "生成的 SQL 无法执行，未向用户返回错误数据"
                        return record
                if is_refusal(record["generated_sql"], rows, TABLES):
                    record["passed"] = True
                    record["reason"] = "正确拒答：未引用业务表，也未返回数据"
                else:
                    record["passed"] = False
                    record["reason"] = (
                        f"捏造答案：引用真实表并返回了 {len(rows or [])} 行数据"
                    )
                return record

            # --- 常规题目：比对结果集 ---
            if agent_error:
                record["reason"] = f"Agent 执行失败: {agent_error[:200]}"
                return record
            if not record["generated_sql"]:
                record["reason"] = "Agent 未产出 SQL"
                return record

            try:
                actual = await DWMySQLRepository(dw_session).execute_sql(
                    record["generated_sql"]
                )
            except Exception as exc:
                record["reason"] = f"生成的 SQL 执行失败: {type(exc).__name__}: {exc}"
                return record

            try:
                expected = await DWMySQLRepository(dw_session).execute_sql(
                    case["reference_sql"]
                )
            except Exception as exc:
                record["reason"] = f"⚠️ 参考 SQL 本身执行失败，请检查评测集: {exc}"
                return record

            if compare_result_sets(actual, expected, case["reference_sql"]):
                record["passed"] = True
                record["reason"] = f"结果集一致（{len(expected)} 行）"
            else:
                record["reason"] = (
                    f"结果集不一致：生成 {len(actual)} 行 / 期望 {len(expected)} 行"
                )
            return record


# --------------------------------------------------------------------- 汇总


def summarize(records: list[dict]) -> dict:
    total = len(records)
    passed = sum(r["passed"] for r in records)
    latencies = sorted(r["latency_ms"] for r in records if r["latency_ms"] is not None)

    by_level: dict[str, dict] = {}
    for r in records:
        bucket = by_level.setdefault(r["level"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(r["passed"])

    refusal = [r for r in records if r["expect_failure"]]
    answerable = [r for r in records if not r["expect_failure"]]

    def pct(n: int, d: int) -> Optional[float]:
        """分母为 0 时返回 None 而非 0.0——没有样本不等于准确率为零。"""
        return round(n / d * 100, 1) if d else None

    def percentile(data: list[int], p: float) -> Optional[int]:
        if not data:
            return None
        idx = min(int(len(data) * p), len(data) - 1)
        return data[idx]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed": passed,
        "accuracy_pct": pct(passed, total),
        "execution_accuracy_pct": pct(
            sum(r["passed"] for r in answerable), len(answerable)
        ),
        "refusal_accuracy_pct": pct(sum(r["passed"] for r in refusal), len(refusal)),
        "by_level": {
            lv: {**v, "accuracy_pct": pct(v["passed"], v["total"])}
            for lv, v in sorted(by_level.items())
        },
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "mean": round(statistics.mean(latencies)) if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
    }


def fmt_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value}%"


def render_markdown(summary: dict, records: list[dict]) -> str:
    lines = [
        "## 评测结果",
        "",
        f"- 评测时间：{summary['generated_at']}",
        f"- 题目总数：{summary['total']}",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 总体准确率 | {fmt_pct(summary['accuracy_pct'])} "
        f"({summary['passed']}/{summary['total']}) |",
        f"| 执行准确率（可答题） | {fmt_pct(summary['execution_accuracy_pct'])} |",
        f"| 拒答准确率（边界题） | {fmt_pct(summary['refusal_accuracy_pct'])} |",
        f"| 端到端延迟 P50 | {summary['latency_ms']['p50']} ms |",
        f"| 端到端延迟 P95 | {summary['latency_ms']['p95']} ms |",
        "",
        "### 分层级准确率",
        "",
        "| 层级 | 通过 / 总数 | 准确率 |",
        "| --- | --- | --- |",
    ]
    for level, v in summary["by_level"].items():
        lines.append(
            f"| {level} | {v['passed']} / {v['total']} | {fmt_pct(v['accuracy_pct'])} |"
        )

    failed = [r for r in records if not r["passed"]]
    if failed:
        lines += ["", "### 未通过用例", "", "| 编号 | 问题 | 原因 |", "| --- | --- | --- |"]
        for r in failed:
            reason = r["reason"].replace("|", "\\|").replace("\n", " ")[:120]
            lines.append(f"| {r['id']} | {r['question']} | {reason} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- 主流程


async def main(config_path: Path, levels: Optional[list[str]], concurrency: int) -> None:
    cases = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if levels:
        cases = [c for c in cases if c["level"] in levels]
    if not cases:
        raise SystemExit("评测集为空，请检查 -c 路径或 --level 过滤条件")

    global TABLES
    TABLES = warehouse_tables()

    embedding_client_manager.init()
    qdrant_client_manager.init()
    es_client_manager.init()
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()

    print(f"开始评测：{len(cases)} 道题，并发度 {concurrency}\n")
    semaphore = asyncio.Semaphore(concurrency)
    try:
        records = await asyncio.gather(
            *(evaluate_one(case, semaphore) for case in cases)
        )
    finally:
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()

    records = sorted(records, key=lambda r: r["id"])
    for r in records:
        print(f"  {'✅' if r['passed'] else '❌'} {r['id']:<7} "
              f"{r['latency_ms']:>6}ms  {r['question']}")
        if not r["passed"]:
            print(f"      └─ {r['reason']}")

    summary = summarize(records)
    print("\n" + "=" * 64)
    print(f"总体准确率      {fmt_pct(summary['accuracy_pct'])}  "
          f"({summary['passed']}/{summary['total']})")
    print(f"执行准确率      {fmt_pct(summary['execution_accuracy_pct'])}")
    print(f"拒答准确率      {fmt_pct(summary['refusal_accuracy_pct'])}")
    print(f"延迟 P50 / P95  {summary['latency_ms']['p50']} / "
          f"{summary['latency_ms']['p95']} ms")
    print("=" * 64)

    reports_dir = PROJECT_ROOT / "evaluation" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = reports_dir / f"report_{stamp}.json"
    json_path.write_text(
        json.dumps({"summary": summary, "records": records},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path = reports_dir / f"report_{stamp}.md"
    md_path.write_text(render_markdown(summary, records), encoding="utf-8")

    print(f"\n完整明细 → {json_path}")
    print(f"README 片段 → {md_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Text2SQL Agent 端到端评测")
    parser.add_argument("-c", "--conf", default="evaluation/golden_set.yaml",
                        help="评测集 YAML 路径")
    parser.add_argument("--level", nargs="*", default=None,
                        help="只跑指定层级，如 --level L1 L2")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="并发题目数，受 LLM 限流影响，默认 2")
    args = parser.parse_args()

    asyncio.run(main(Path(args.conf), args.level, args.concurrency))
