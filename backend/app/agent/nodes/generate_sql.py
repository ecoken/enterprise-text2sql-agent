import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# 模型判定无法作答时输出的前缀标记，与 prompts/generate_sql.prompt 中的约定一致。
# 用固定标记而非让模型自由发挥，是为了让下游能可靠识别——
# 靠正则匹配"抱歉""无法"这类自然语言措辞不可靠，模型换个说法就漏了。
CANNOT_ANSWER_PREFIX = "CANNOT_ANSWER"


async def generate_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "生成SQL", "status": "running"})

    query = state["query"]
    table_infos = state["table_infos"]
    metric_infos = state["metric_infos"]
    date_info = state["date_info"]
    db_info = state["db_info"]

    try:
        prompt = PromptTemplate(template=load_prompt("generate_sql"),
                                input_variables=["query", "table_infos", "metric_infos", "date_info", "db_info"])
        output_parser = StrOutputParser()

        chain = prompt | llm | output_parser

        result = await chain.ainvoke(
            {"query": query,
             "table_infos": yaml.dump(table_infos, allow_unicode=True, sort_keys=False),
             "metric_infos": yaml.dump(metric_infos, allow_unicode=True, sort_keys=False),
             "date_info": yaml.dump(date_info, allow_unicode=True, sort_keys=False),
             "db_info": yaml.dump(db_info, allow_unicode=True, sort_keys=False)
             })

        result = (result or "").strip()

        # 模型判定所需维度/指标在召回结果中不存在时，会返回 CANNOT_ANSWER 标记。
        # 这条分支存在的意义：与其让模型用近似字段凑一条能执行的 SQL，
        # 不如明确告诉用户答不了。Text2SQL 最危险的失败不是查不出来，
        # 而是返回一张看着合理、实则张冠李戴的结果表。
        if result.upper().startswith(CANNOT_ANSWER_PREFIX):
            reason = result[len(CANNOT_ANSWER_PREFIX):].strip(" :：") or "所需的维度或指标不存在"
            writer({"type": "progress", "step": "生成SQL", "status": "success"})
            writer({"type": "error", "message": f"无法回答：{reason}"})
            logger.warning(f"模型判定无法作答：{reason}")
            return {"sql": result, "cannot_answer": reason}

        writer({"type": "progress", "step": "生成SQL", "status": "success"})
        logger.info(f"生成的SQL: {result}")
        return {"sql": result, "cannot_answer": None}
    except Exception as e:
        writer({"type": "progress", "step": "生成SQL", "status": "error"})
        logger.error(f"生成SQL失败: {str(e)}")
        raise
