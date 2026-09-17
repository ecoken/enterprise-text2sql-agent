import asyncio

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.entities.column_info import ColumnInfo
from app.prompt.prompt_loader import load_prompt

# 单个节点内并发召回的关键词数上限。
# 这一路每个关键词都要先过 Embedding 服务再查 Qdrant，
# 取值受限于 TEI 单实例的吞吐；调大只会让请求在服务端排队。
RECALL_CONCURRENCY = 6


async def recall_column(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "召回字段", "status": "running"})

    query = state["query"]
    keywords = state["keywords"]

    embedding_client = runtime.context["embedding_client"]
    column_qdrant_repository = runtime.context["column_qdrant_repository"]

    try:
        # 使用LLM扩展关键词
        prompt = PromptTemplate(
            template=load_prompt("extend_keywords_for_column_recall"),
            input_variables=["query"],
        )
        output_parser = JsonOutputParser()

        chain = prompt | llm | output_parser

        result = await chain.ainvoke({"query": query})

        # 使用扩展后的关键词召回字段信息
        retrieved_columns_map: dict[str, ColumnInfo] = {}

        keywords = list(set(keywords + result))
        logger.info(f"召回字段信息扩展关键词：{keywords}")

        semaphore = asyncio.Semaphore(RECALL_CONCURRENCY)

        async def recall_one(keyword: str) -> list[ColumnInfo]:
            async with semaphore:
                embedding = await embedding_client.aembed_query(keyword)
                return await column_qdrant_repository.search(embedding)

        # 并发召回：串行版本的耗时是所有关键词之和，关键词扩展后常有 8~12 个，
        # 延迟随之线性增长。并发后整体耗时取决于最慢的那一路。
        # 用信号量限制并发数而非无节制放开——Embedding 服务单实例吞吐有限，
        # 一次涌入十几个请求只会在服务端排队，并不会更快。
        # return_exceptions 让单个关键词失败不牵连其余关键词。
        payload_groups = await asyncio.gather(
            *(recall_one(k) for k in keywords), return_exceptions=True
        )

        # gather 保持入参顺序，因此这里的「先到先得」去重行为与串行版本完全一致
        for keyword, payloads in zip(keywords, payload_groups):
            if isinstance(payloads, BaseException):
                logger.warning(f"关键词[{keyword}]召回字段失败，跳过该词：{payloads}")
                continue
            for payload in payloads:
                column_id = payload.id
                if column_id not in retrieved_columns_map:
                    retrieved_columns_map[column_id] = payload

        retrieved_columns = list(retrieved_columns_map.values())

        writer({"type": "progress", "step": "召回字段", "status": "success"})
        logger.info(f"召回字段信息：{list(retrieved_columns_map.keys())}")
        return {"retrieved_columns": retrieved_columns}
    except Exception as e:
        writer({"type": "progress", "step": "召回字段", "status": "error"})
        logger.error(f"召回字段信息失败: {str(e)}")
        raise
