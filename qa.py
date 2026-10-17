import json

import requests

import config
import retriever


def _normalize_options(options) -> list[dict]:
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            return []
    if not isinstance(options, list):
        return []
    return [option for option in options if isinstance(option, dict)]


DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
REQUEST_TIMEOUT = 30


def build_prompt(question, references) -> str:
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

    material_text = "\n\n".join(materials) if materials else "无"
    return (
        "请只依据下面提供的材料回答问题。\n\n"
        "要求：\n"
        "1. 只依据材料回答，不得使用你自己的知识补充、推断或编造。\n"
        "2. 如果材料中没有相关内容，必须直接回答“材料中没有相关内容”。\n"
        "3. 必须使用中文，回答保持简洁。\n\n"
        f"用户问题：{question}\n\n"
        f"材料：\n{material_text}"
    )


def _public_references(references) -> list[dict]:
    return [
        {
            "id": reference.get("id"),
            "stem": reference.get("stem"),
            "answer": reference.get("answer"),
        }
        for reference in references
    ]


def ask(question, top_k=5) -> dict:
    try:
        search_results = retriever.search(question, top_k=top_k)
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

    prompt = build_prompt(question, search_results)
    request_body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            DEEPSEEK_API_URL,
            headers=headers,
            json=request_body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.Timeout:
        return {
            "answer": "请求大模型服务超时，请稍后重试",
            "references": references,
        }
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"（HTTP {status_code}）" if status_code else ""
        return {
            "answer": f"大模型服务返回错误{detail}，请稍后重试",
            "references": references,
        }
    except requests.RequestException:
        return {
            "answer": "请求大模型服务失败，请检查网络后重试",
            "references": references,
        }
    except Exception:
        return {
            "answer": "请求大模型服务失败，请稍后重试",
            "references": references,
        }

    try:
        response_data = response.json()
        answer = response_data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {
            "answer": "大模型服务返回数据格式异常，请稍后重试",
            "references": references,
        }

    if not isinstance(answer, str) or not answer.strip():
        return {
            "answer": "大模型服务返回数据格式异常，请稍后重试",
            "references": references,
        }

    return {"answer": answer.strip(), "references": references}
