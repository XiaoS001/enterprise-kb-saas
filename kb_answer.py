"""知识库问答的作答层：用检索到的证据让模型写答案，并强制引用。

## 为什么不能复用 `agent.run_agent`

🔴 实测发现的架构 bug：

`kb_api._ask_via_agent(question, evidence)` **收下了 evidence 却从没用过**，
直接 `agent.run_agent(question)` —— 而 agent 内部走 `retriever_lc`，
查的是 XM03 带来的 `data/questions.json`（685 道考试题），**不是用户的知识库**。

后果：
  · 答案来自考试题库 → 与用户问题无关 → 模型回「材料中没有相关内容」
  · 引用卡片来自我们自己的 BM25 → 指向《故障处理预案》第 0 段
  · **两者完全脱节**，而且 `degraded` 被标成 True（因为 critic 报错）

这个 bug 只有真调才暴露：单测全部 mock 掉模型，`evidence` 参数是死的也没人发现。

## 这个模块做什么

把「检索到的证据」直接喂给模型，用**知识库专用提示词**作答：
  · 只依据材料，不许用模型自己的知识
  · 每个结论必须用 `[doc:<id>]` 标注依据（与 `check_citation` 的正则对齐）
  · 材料里没有就说「知识库中没有相关内容」，不编造

模型与端点从 config 读（阶段 1 的教训：硬编码 model 且不传 api_base 会必然 401）。
"""
import logging

from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek

import config


logger = logging.getLogger('rmb01.answer')

REQUEST_TIMEOUT = 30

# 🔴 提示词里不能出现裸的 `{...}`：ChatPromptTemplate 会把它当模板变量。
#    （`prompts/__init__.py` 的 EVALUATE_PROMPT 就栽在 `{"enough": ...}` 上。）
#    这里只用 `[doc:<id>]` 这种方括号格式，安全。
KB_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是企业知识库问答助手。你只能依据用户提供的材料作答。\n"
            "要求：\n"
            "1. 只能使用材料中的内容，不得使用你自己的知识补充、推断或编造。\n"
            "2. 每个结论后面必须标注依据，格式为 [doc:<id>]，"
            "其中 <id> 取材料开头标注的 [doc:<id>] 里的数字。\n"
            "3. 如果材料中没有能回答该问题的内容，直接回答"
            "「知识库中没有相关内容」，不要编造，也不要加引用。\n"
            "4. 使用中文，回答简洁，不要复述材料原文的全部内容。",
        ),
        ("human", "用户问题：{question}\n\n材料：\n{materials}"),
    ]
)


def answer_from_evidence(question, materials, api_key=None):
    """用给定材料回答问题。

    返回 (answer, reasons)：
      · 成功 → (答案文本, [])
      · 失败 → ('', [降级原因])

    🔴 失败一律降级，不抛 —— 上层接口不能因为模型故障返 500。
    """

    key = api_key if api_key is not None else config.api_key()
    if not key:
        return '', ['未配置 API 密钥']

    if not materials or not materials.strip():
        return '', ['没有可用的检索材料']

    try:
        model = ChatDeepSeek(
            model=config.AI_MODEL,
            api_key=key,
            api_base=config.AI_BASE_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response = (KB_PROMPT | model).invoke({
            'question': question,
            'materials': materials,
        })
        content = response.content
        if not isinstance(content, str) or not content.strip():
            return '', ['模型返回内容为空']
        return content.strip(), []
    except Exception as exc:
        logger.warning('answer_from_evidence_failed', extra={
            'event': 'answer_from_evidence_failed',
            'error': f'{type(exc).__name__}: {str(exc)[:300]}',
        })
        return '', [f'模型调用失败: {type(exc).__name__}: {str(exc)[:200]}']