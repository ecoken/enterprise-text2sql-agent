import asyncio

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.entities.value_info import ValueInfo
from app.prompt.prompt_loader import load_prompt

# 单节点内并发召回的关键词数上限。
# 这一路直连 Elasticsearch，不经过 Embedding 服务，单请求成本低，
# 因此并发度设得比另外两路高。
RECALL_CONCURRENCY = 10


async def recall_value(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "召回字段取值", "status": "running"})

    query = state["query"]
    keywords = state["keywords"]

    value_es_repository = runtime.context["value_es_repository"]

    try:
        # 使用LLM扩展关键词
        prompt = PromptTemplate(template=load_prompt("extend_keywords_for_value_recall"), input_variables=["query"])
        output_parser = JsonOutputParser()

        chain = prompt | llm | output_parser

        result = await chain.ainvoke({"query": query})

        # 使用扩展后的关键词召回字段取值
        values_map: dict[str, ValueInfo] = {}
        keywords = list(set(keywords + result))
        logger.info(f"召回字段取值扩展关键词：{keywords}")

        semaphore = asyncio.Semaphore(RECALL_CONCURRENCY)

        async def recall_one(keyword: str) -> list[ValueInfo]:
            async with semaphore:
                return await value_es_repository.search(keyword)

        # 并发召回。这一路只查 Elasticsearch、不经过 Embedding 服务，
        # 单次请求轻得多，因此并发度可以放得比另外两路高。
        value_groups = await asyncio.gather(
            *(recall_one(k) for k in keywords), return_exceptions=True
        )

        for keyword, values in zip(keywords, value_groups):
            if isinstance(values, BaseException):
                logger.warning(f"关键词[{keyword}]召回取值失败，跳过该词：{values}")
                continue
            for value in values:
                value_id = value.id
                if value_id not in values_map:
                    values_map[value_id] = value

        retrieved_values = list(values_map.values())

        writer({"type": "progress", "step": "召回字段取值", "status": "success"})
        logger.info(f"召回字段取值：{list(values_map.keys())}")

        return {'retrieved_values': retrieved_values}
    except Exception as e:
        writer({"type": "progress", "step": "召回字段取值", "status": "error"})
        logger.error(f"召回字段取值失败: {str(e)}")
        raise
