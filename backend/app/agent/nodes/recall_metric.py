import asyncio

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.entities.metric_info import MetricInfo
from app.prompt.prompt_loader import load_prompt

# 单节点内并发召回的关键词数上限。这一路同样要过 Embedding 服务，
# 与 recall_column 共享同一个 TEI 实例，取值保持一致。
RECALL_CONCURRENCY = 6


async def recall_metric(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "召回指标", "status": "running"})

    query = state["query"]
    keywords = state["keywords"]

    embedding_client = runtime.context['embedding_client']
    metric_qdrant_repository = runtime.context['metric_qdrant_repository']

    try:
        # 使用LLM扩展关键词
        prompt = PromptTemplate(template=load_prompt("extend_keywords_for_metric_recall"), input_variables=["query"])
        output_parser = JsonOutputParser()

        chain = prompt | llm | output_parser

        result = await chain.ainvoke({"query": query})

        # 使用扩展后的关键词召回指标信息
        retrieved_metrics_map: dict[str, MetricInfo] = {}

        keywords = list(set(keywords + result))
        logger.info(f"召回指标信息扩展关键词：{keywords}")

        semaphore = asyncio.Semaphore(RECALL_CONCURRENCY)

        async def recall_one(keyword: str) -> list[MetricInfo]:
            async with semaphore:
                embedding = await embedding_client.aembed_query(keyword)
                return await metric_qdrant_repository.search(embedding)

        # 并发召回，理由同 recall_column：串行耗时是所有关键词之和，
        # 并发后取决于最慢的一路。信号量防止一次性压垮 Embedding 服务。
        payload_groups = await asyncio.gather(
            *(recall_one(k) for k in keywords), return_exceptions=True
        )

        for keyword, payloads in zip(keywords, payload_groups):
            if isinstance(payloads, BaseException):
                logger.warning(f"关键词[{keyword}]召回指标失败，跳过该词：{payloads}")
                continue
            for payload in payloads:
                metric_id = payload.id
                if metric_id not in retrieved_metrics_map:
                    retrieved_metrics_map[metric_id] = payload

        retrieved_metrics = list(retrieved_metrics_map.values())

        writer({"type": "progress", "step": "召回指标", "status": "success"})
        logger.info(f"召回指标信息：{list(retrieved_metrics_map.keys())}")
        return {"retrieved_metrics": retrieved_metrics}
    except Exception as e:
        writer({"type": "progress", "step": "召回指标", "status": "error"})
        logger.error(f"召回指标信息失败: {str(e)}")
        raise
