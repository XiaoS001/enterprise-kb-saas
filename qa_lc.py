import json

from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek

import config
import retriever_lc


DEEPSEEK_MODEL = "deepseek-chat"
REQUEST_TIMEOUT = 30

PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "请只依据用户提供的材料回答问题。\n"
            "要求：\n"
            "1. 只依据材料回答，不得使用你自己的知识补充、推断或编造。\n"
            "2. 如果材料中没有相关内容，必须直接回答“材料中没有相关内容”。\n"
            "3. 必须使用中文，回答保持简洁。",
        ),
        ("human", "用户问题：{question}\n\n材料：\n{materials}"),
    ]
)


def _normalize_options(options) -> list[dict]:
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            return []
    if not isinstance(options, list):
        return []
    return [option for option in options if isinstance(option, dict)]


def _format_materials(references) -> str:
    materials = []
    for index, reference in enumerate(references, start=1):
        options = _normalize_options(reference.get("options"))
        option_text = "   ".join(
            f"{option.get('letter') or ''}. {option.get('text') or ''}"
            for option in options
        )
        materials.append(
            f"材料 {index}：\n"
            f"题目：{reference.get('stem') or ''}\n"
            f"选项：{option_text or '无'}\n"
            f"答案：{reference.get('answer') or ''}\n"
            f"解析：{reference.get('explanation') or '无'}"
        )
    return "\n\n".join(materials) if materials else "无"


def _public_references(references) -> list[dict]:
    return [
        {
            "id": reference.get("id"),
            "stem": reference.get("stem"),
            "answer": reference.get("answer"),
        }
        for reference in references
    ]


def _invoke_model(question, materials, api_key) -> str:
    model = ChatDeepSeek(
        model=DEEPSEEK_MODEL,
        api_key=api_key,
        timeout=REQUEST_TIMEOUT,
    )
    chain = PROMPT | model
    response = chain.invoke({"question": question, "materials": materials})
    answer = response.content

    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("模型返回内容为空")

    return answer.strip()


def ask(question, top_k=5) -> dict:
    try:
        search_results = retriever_lc.search(question, top_k=top_k)
    except Exception:
        return {"answer": "检索题目材料失败，请稍后重试", "references": []}

    references = _public_references(search_results)
    if not search_results:
        return {"answer": "没有找到相关题目材料", "references": []}

    api_key = config.api_key()
    if not api_key:
        return {
            "answer": "服务未配置 API 密钥，暂时无法回答",
            "references": references,
        }

    try:
        answer = _invoke_model(question, _format_materials(search_results), api_key)
    except Exception:
        return {
            "answer": "请求大模型服务失败，请稍后重试",
            "references": references,
        }

    return {"answer": answer, "references": references}
