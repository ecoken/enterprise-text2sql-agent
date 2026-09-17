# Enterprise Text2SQL Agent

> 面向企业数仓的自然语言问数系统。用户用中文提问，Agent 自动完成元数据召回、SQL 生成、校验纠错与执行，流式返回查询结果。

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.0-1C3C3C)
![FastAPI](https://img.shields.io/badge/FastAPI-0.128-009688?logo=fastapi&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vuedotjs&logoColor=white)
![Qdrant](https://img.shields.io/badge/Qdrant-向量检索-DC244C)
![Elasticsearch](https://img.shields.io/badge/Elasticsearch-8-005571?logo=elasticsearch&logoColor=white)

<!-- 录好演示 GIF 后，把下面这行的注释符去掉即可 -->
<!-- ![演示](docs/demo.gif) -->

---

## 这个项目解决什么问题

业务人员想看数据，但不会写 SQL；数据分析师被大量重复的取数需求淹没。

直接把「表结构 + 用户问题」丢给大模型让它生成 SQL，在玩具场景能跑通，在真实数仓里会立刻崩掉：

| 真实场景的难点 | 本项目的应对 |
| --- | --- |
| 数仓有几百张表，全部塞进 Prompt 会超长且干扰模型 | 两级召回：先粗召回候选，再由 LLM 精排裁剪 |
| 用户说「销售额」，数仓字段叫 `gmv` | LLM 关键词扩展 + 别名库，跨越口语与数仓术语的语义鸿沟 |
| 用户说「华东地区」，需要知道这是 `region_name` 的一个**取值** | 字段取值单独建全文索引，与字段名检索分离 |
| 「去年」「上个季度」模型无法推算 | 运行时注入当前日期、星期、季度 |
| 模型生成的 SQL 有语法错或字段不存在 | 真实数据库校验，失败后带错误信息重新生成 |
| 一次查询要走十几步，用户干等着不知道进度 | 每个节点通过 SSE 推送执行状态，前端实时渲染 |

---

## 系统架构

### 在线查询链路

基于 LangGraph 编排的 12 节点有向图，包含并行扇出、条件分支两种拓扑：

```mermaid
flowchart TD
    A([用户提问]) --> EK["extract_keywords<br/>jieba 词性过滤分词"]

    EK --> RC["recall_column<br/>向量检索 · Qdrant"]
    EK --> RV["recall_value<br/>全文检索 · Elasticsearch"]
    EK --> RM["recall_metric<br/>向量检索 · Qdrant"]

    RC --> MG["merge_retrieved_info<br/>三路结果合并去重"]
    RV --> MG
    RM --> MG

    MG --> FT["filter_table<br/>LLM 精排裁剪表结构"]
    MG --> FM["filter_metric<br/>LLM 精排裁剪指标"]

    FT --> AC["add_extra_context<br/>注入日期 / 数据库方言"]
    FM --> AC

    AC --> GS["generate_sql<br/>LLM 生成 SQL"]
    GS --> VS{"validate_sql<br/>真实库校验"}

    VS -->|通过| EX["execute_sql"]
    VS -->|失败| CS["correct_sql<br/>携带错误信息重新生成"]
    CS --> EX

    EX --> Z([流式返回结果表格])
```

### 离线元知识库构建

查询链路依赖的元数据由 `build_meta_knowledge` 脚本离线构建，三类知识分别落到最适合它的存储：

| 知识类型 | 存储 | 检索方式 | 为什么这样选 |
| --- | --- | --- | --- |
| 表 / 字段的语义描述与别名 | Qdrant | 向量相似度 | 字段名是**语义**的，「销售额」和 `gmv` 字面零重合但语义相近 |
| 字段的实际取值 | Elasticsearch | 全文检索 | 取值是**字面**的，「华东」必须精确命中，向量化反而引入噪声 |
| 业务指标定义与口径 | Qdrant | 向量相似度 | 指标名同样是语义匹配问题 |
| 元数据结构化信息 | MySQL (`meta`) | 主键查询 | 召回后按 ID 回表拿完整定义 |

> **按数据性质选择检索方式**是本项目的核心设计决策之一。把字段取值也做向量化，会让「华东」召回出「华南」「东北」这类语义相近但业务上完全错误的结果。

---

## 技术栈

| 分层 | 选型 |
| --- | --- |
| Agent 编排 | LangGraph 1.0（`StateGraph` + `context_schema` 依赖注入 + `stream_writer` 自定义流） |
| 大模型 | DeepSeek（OpenAI 兼容接口，可替换为通义 / 智谱 / 任意兼容服务） |
| Embedding | BAAI/bge-large-zh-v1.5，经 HuggingFace TEI 本地部署 |
| 向量库 | Qdrant |
| 全文检索 | Elasticsearch 8 |
| 关系库 | MySQL 8（`meta` 元数据库 + `dw` 数仓库双数据源） |
| 分词 | jieba（按词性白名单过滤） |
| 后端 | FastAPI + SQLAlchemy 2.0 async + asyncmy |
| 前端 | Vue 3 + Vite（SSE 流式渲染） |
| 日志 | loguru + `contextvars` 实现请求级 `request_id` 追踪 |
| 配置 | OmegaConf 结构化配置 + 环境变量注入 |

---

## 快速开始

### 1. 准备依赖服务

```bash
# Qdrant
docker run -d -p 6333:6333 --name qdrant qdrant/qdrant

# Elasticsearch 8
docker run -d -p 9200:9200 --name es \
  -e "discovery.type=single-node" -e "xpack.security.enabled=false" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.0

# Embedding 服务（TEI）
docker run -d -p 8081:80 --name tei \
  ghcr.io/huggingface/text-embeddings-inference:cpu-latest \
  --model-id BAAI/bge-large-zh-v1.5
```

MySQL 需自行准备，并创建 `meta`（元数据）与 `dw`（数仓）两个库。

### 2. 配置环境变量

```bash
cd backend
cp .env.example .env
```

编辑 `.env` 填入数据库账号与大模型 API Key：

```ini
DB_META_USER=your_mysql_user
DB_META_PASSWORD=your_mysql_password
DB_DW_USER=your_mysql_user
DB_DW_PASSWORD=your_mysql_password

LLM_MODEL_NAME=deepseek-flash
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
```

> 服务地址、端口等非敏感项在 `conf/app_config.yaml` 中配置。所有凭据均通过 `${oc.env:...}` 从环境变量注入，仓库内不含任何真实密钥。

### 3. 构建元知识库

```bash
uv sync
uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml
```

该脚本读取 `conf/meta_config.yaml` 中的表结构、字段别名与指标定义，写入 MySQL 元数据库，并将字段 / 指标向量化入 Qdrant、字段取值同步入 Elasticsearch。

### 4. 启动服务

```bash
# 后端 → http://localhost:8000
uv run python main.py

# 前端 → http://localhost:5173
cd ../frontend
npm install
npm run dev
```

打开前端页面即可提问，例如：**「统计去年各地区的销售总额」**。

---

## 项目结构

```
├── backend/
│   ├── app/
│   │   ├── agent/            # LangGraph 编排
│   │   │   ├── graph.py      #   图定义：节点、边、条件分支
│   │   │   ├── state.py      #   状态 Schema
│   │   │   └── nodes/        #   12 个节点实现
│   │   ├── api/              # FastAPI 路由、请求模型、依赖注入
│   │   ├── clients/          # 外部服务连接管理（生命周期 init/close）
│   │   ├── repositories/     # 数据访问层，按存储拆分
│   │   ├── services/         # 业务编排层
│   │   ├── conf/             # 配置 Schema 与加载
│   │   └── core/             # 日志、请求上下文
│   ├── conf/                 # YAML 配置与数仓元数据定义
│   ├── prompts/              # Prompt 模板（与代码分离，便于迭代）
│   └── main.py               # 应用入口 + request_id 中间件
└── frontend/                 # Vue 3 聊天界面
```

---

## 工程实践

- **分层清晰**：`clients` 管连接生命周期、`repositories` 管数据访问、`services` 管业务编排、`api` 管协议适配，跨层调用通过依赖注入完成。
- **双数据源隔离**：元数据库与数仓库使用独立的连接池与 Session 工厂，避免误操作跨库写入。
- **Prompt 与代码分离**：全部 Prompt 存放于 `prompts/` 目录，调整措辞无需改动代码、无需重启进程。
- **配置外部化**：敏感凭据一律经环境变量注入，仓库提供 `.env.example` 模板，`.env` 由 `.gitignore` 拦截。
- **请求级链路追踪**：中间件为每个请求生成 `request_id`，经 `contextvars` 透传至全部日志，多请求并发时日志不串。
- **全链路异步**：数据库、向量库、ES、LLM 调用均为 async，单进程即可支撑并发查询。

---

## 评测结果

项目内置 37 道黄金评测集（`backend/evaluation/golden_set.yaml`），按能力维度分 8 层，
覆盖基础聚合、多表关联、相对时间推算、指标口径、字面取值召回、排序 TopN，
以及**边界拒答**——即数仓中不存在的字段或指标，Agent 是否会捏造答案。

评分采用**执行准确率**：将 Agent 生成的 SQL 与参考 SQL 分别执行后比对结果集，
而非比对 SQL 文本，因为同一问题存在大量等价写法。比对规则允许 Agent 多返回列
（参考 SQL 给出的是最小正确答案），但保留行内各列的对应关系。

```bash
uv run python -m app.scripts.run_evaluation -c evaluation/golden_set.yaml
```

### 基线（2026-09-18）

| 指标 | 数值 |
| --- | --- |
| 总体准确率 | **91.9%**（34 / 37） |
| 执行准确率（33 道可答题） | **93.9%** |
| 拒答准确率（4 道边界题） | **75.0%** |
| 端到端延迟 P50 | 17.0 s |
| 端到端延迟 P95 | 68.4 s |

| 层级 | L1 | L2 | L3 | L4 | L5 | L6 | L7 | L8 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 基础聚合 / 单维 / 多维 / 相对时间 / 指标口径 / 取值召回 / 排序 / 拒答 | 5/5 | 5/5 | 5/5 | 3/5 | 4/4 | 5/5 | 4/4 | 3/4 |

> 以上为单次完整运行的结果，尚未做多轮重复测量，因此不代表统计意义上的稳定值——
> 已观察到同一用例在不同运行间结论翻转（LLM 即便 `temperature=0` 亦非完全确定）。
> 延迟在并发 4 的条件下测得，不等同于单请求延迟。

### 已定位的失败模式

**1. 按代理键分组导致业务维度被拆分**（L4-03、L7-04）

问「各大区的销售额」时，模型生成 `GROUP BY region_id, region_name`。
但 `dim_region` 中「华东」对应浙江、上海两条记录，按代理键分组会把华东拆成两行，
正确做法是按业务属性 `region_name` 聚合。这是维度建模场景下 Text2SQL 的典型陷阱。

**2. 把已有维度冒充为不存在的维度**（L8-03）

问「各门店的销售额排名」，数仓中并无门店维度，模型却生成：

```sql
SELECT r.region_name AS 门店, SUM(o.order_amount) AS 销售额 ...
```

把大区数据起别名叫「门店」返回。**这是全部失败用例中危害最大的一类**——
SQL 能执行、结果看着合理，业务方拿着一张张冠李戴的报表去做决策。
相比之下答不出来反而是安全的。另外三道边界题模型均正确拒答
（返回说明文案或空集，未触碰任何业务表）。

**3. 输出表示差异**（L4-05）

问「去年每个月的销售额」，模型返回 `'2025-01'` 格式而参考答案为月份数字 `1`，
两者语义等价。这是评测口径的局限而非模型缺陷，此处记为未通过以保持判定标准一致。

---

## Roadmap

- [x] **评测体系**：37 道黄金评测集 + 执行准确率自动化评测，支持分层级与按题过滤，
      输出 JSON 明细与 Markdown 报告
- [ ] **维度分组修正**：在 Prompt 中明确「按业务属性而非代理键聚合」，解决 L4-03、L7-04
      暴露的大区被拆分问题
- [ ] **显式拒答机制**：为 `generate_sql` 增加前置校验，当召回结果中不存在问题所指的维度或
      指标时直接终止并返回说明，而不是放任模型用近似字段凑答案（针对 L8-03）
- [ ] **并行召回优化**：当前多关键词 Embedding 为串行循环，改造为 `asyncio.gather` 并发，
      降低召回阶段延迟
- [ ] **纠错闭环**：`correct_sql` 后回流至 `validate_sql` 形成带上限的重试循环，
      替代当前的单次纠错
- [ ] **多轮评测与稳定性度量**：同一评测集重复运行取均值与波动区间，
      替代当前的单次测量
- [ ] **模型分级路由**：SQL 生成与纠错节点使用强模型，关键词扩展等轻量节点使用快模型，
      降低单次查询成本
- [ ] **语义缓存**：对高相似度的重复提问复用历史结果
