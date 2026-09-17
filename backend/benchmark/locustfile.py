"""压力测试：对 /api/query 施加并发负载，观测吞吐与延迟。

与评测脚本的分工：
- `run_evaluation.py` 回答「答得准不准」，单线程语义，关心正确性
- 本脚本回答「扛不扛得住」，关心 QPS、延迟分位数、错误率

**为什么要单独压测而不是看评测时的延迟**：评测跑的是固定的 37 道题，
且为了避免限流刻意压低了并发，测出的延迟是「空载下的单请求耗时」。
而真实场景是多个用户同时提问，此时连接池争用、LLM 限流、
向量库并发查询都会显现出来，这些在低并发评测里完全看不到。

**这个接口的特殊性**：响应是 SSE 流式的，一次请求会持续推送执行进度直到结束。
所以「响应时间」必须计到流读完为止，只测首字节毫无意义——
它只反映 FastAPI 接收请求的速度，不反映 Agent 真正跑完一次查询要多久。

用法：
    # 有界面，浏览器打开 http://localhost:8089 调节并发
    locust -f benchmark/locustfile.py --host http://localhost:8000

    # 无界面，10 并发跑 3 分钟后自动出报告
    locust -f benchmark/locustfile.py --host http://localhost:8000 \
        --headless -u 10 -r 2 -t 3m --csv benchmark/results/run

注意：每次请求都会真实调用大模型，10 并发跑 3 分钟的 API 成本不低，
先用 -u 2 小规模验证链路通畅，再逐步加压。
"""

import json
import random
import time

from locust import HttpUser, between, events, task

# 压测用的问题池。刻意混合不同复杂度：
# 简单聚合走的链路短，多表关联要多轮 LLM 调用，两者的耗时差异很大。
# 只用简单问题压测会把系统的真实压力估得过于乐观。
QUERIES = [
    # 短链路：单表聚合
    "总共有多少笔订单",
    "所有订单的销售总额是多少",
    "一共卖出了多少件商品",
    # 中链路：单维度关联
    "各个大区的销售总额分别是多少",
    "每个商品类别卖了多少件",
    "不同会员等级的客户贡献了多少销售额",
    # 长链路：多维关联 + 相对时间
    "去年各大区黄金会员的销售额",
    "每个季度不同品牌的销量",
    "去年销售额最高的品类是哪个",
    # 取值召回：额外走一次 ES
    "华东地区卖了多少钱",
    "女性客户买得最多的品类是什么",
]

# 单次请求的超时上限。Agent 链路含多次 LLM 调用，
# 高并发下排队会显著拉长，设太短会把「慢」误报成「错」。
REQUEST_TIMEOUT = 180


class QueryUser(HttpUser):
    """模拟一个持续提问的用户。"""

    # 思考时间：真实用户看完结果才会问下一个问题，不会零间隔轰炸。
    # 不设这个的话压出来的是「接口极限」而非「可支撑的用户数」。
    wait_time = between(3, 8)

    @task
    def ask_question(self) -> None:
        query = random.choice(QUERIES)
        started = time.perf_counter()

        with self.client.post(
            "/api/query",
            json={"query": query},
            stream=True,
            timeout=REQUEST_TIMEOUT,
            catch_response=True,
            name="/api/query",
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return

            got_result = False
            error_message = None
            try:
                # SSE 流：必须读到流结束，中途断开等于没测完整链路
                for raw in response.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "result":
                        got_result = True
                    elif payload.get("type") == "error":
                        error_message = payload.get("message", "未知错误")
            except Exception as exc:
                response.failure(f"读取流失败: {type(exc).__name__}: {exc}")
                return

            elapsed = (time.perf_counter() - started) * 1000
            if error_message:
                # Agent 内部报错：HTTP 是 200，但业务上失败了。
                # 不标记 failure 的话错误率会显示为 0，压测结论完全失真。
                response.failure(f"Agent 报错: {error_message[:80]}")
            elif not got_result:
                response.failure("流正常结束但未收到 result 事件")
            else:
                response.success()
                if elapsed > 60_000:
                    # 超过一分钟虽然算成功，但已经不具备可用性，单独记一笔便于复盘
                    events.request.fire(
                        request_type="SLOW", name="/api/query 超 60s",
                        response_time=elapsed, response_length=0, exception=None,
                    )


@events.test_start.add_listener
def on_start(environment, **kwargs) -> None:
    print(f"压测开始，目标：{environment.host}")
    print(f"问题池 {len(QUERIES)} 条，覆盖短/中/长三种链路长度")
    print("提示：每次请求都会真实调用大模型，注意 API 成本")


@events.test_stop.add_listener
def on_stop(environment, **kwargs) -> None:
    stats = environment.stats.total
    print("\n" + "=" * 60)
    print(f"总请求  {stats.num_requests}   失败 {stats.num_failures}"
          f"（{stats.fail_ratio:.1%}）")
    print(f"吞吐    {stats.total_rps:.2f} req/s")
    print(f"延迟    P50 {stats.get_response_time_percentile(0.5):.0f}ms   "
          f"P95 {stats.get_response_time_percentile(0.95):.0f}ms   "
          f"P99 {stats.get_response_time_percentile(0.99):.0f}ms")
    print("=" * 60)
