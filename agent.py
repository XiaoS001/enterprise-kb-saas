import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

import config as app_config
import qa_lc
import retriever_lc
from prompts import EVALUATE_PROMPT, PROMPT_VERSIONS, REWRITE_PROMPT
from skills.check_citation import skill as check_citation_skill
from skills.get_question import (
    set_default_question_source,
    skill as get_question_skill,
)
from skills.registry import registry
from skills.search_questions import (
    set_default_retriever,
    skill as search_questions_skill,
)


# 🔴 模型名与端点**不再硬编码**，一律从 config 读。
#
# 阶段 1 的任务单只写了「收口 key」，漏了 model 与 base_url ——
# 后果是：无论配什么 key 都必然 401，因为
#   · model 写死 "deepseek-chat"，不认 config.AI_MODEL
#   · ChatDeepSeek 没传 api_base，默认打官方 api.deepseek.com
# 换成任何第三方 OpenAI 兼容端点都会失败，而报错是 401，
# 看起来像「key 不对」，实际是端点压根没传进去。
DEEPSEEK_MODEL = "deepseek-chat"      # 仅作兜底常量，实际调用读 app_config.AI_MODEL
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
MAX_ROUNDS = 12
TOP_K = 5
STATE_DB_PATH = Path(__file__).resolve().parent / "data" / "agent_state.db"

logger = logging.getLogger(__name__)

_CHECKPOINTER: Any = None
_CHECKPOINTER_BACKEND = "uninitialized"
CHECKPOINTED_AGENT_GRAPH: Any = None


class AgentState(TypedDict, total=False):
    question: str
    thread_id: str
    conversation_history: list[str]
    queries: list[str]
    documents: list[dict]
    verdict: Literal["够", "不够", ""]
    answer: str
    attempts: int
    last_agent: str
    next_agent: str
    route_history: list[str]
    agent_counts: dict[str, int]
    skill_counts: dict[str, int]
    supervisor_rounds: int
    degraded: bool
    degradation_reasons: list[str]
    prompt_versions: dict[str, str]
    citation_check: dict[str, Any]
    citation_history: list[dict[str, Any]]
    citations: list[str]
    invalid_citations: list[str]
    citation_retry_count: int


def _register_default_skills() -> None:
    set_default_retriever(retriever_lc)
    set_default_question_source(retriever_lc)
    for skill in (
        search_questions_skill,
        get_question_skill,
        check_citation_skill,
    ):
        try:
            registry.register(skill)
        except ValueError:
            pass


_register_default_skills()


def _get_checkpointer() -> tuple[Any, str]:
    global _CHECKPOINTER, _CHECKPOINTER_BACKEND

    if _CHECKPOINTER is not None:
        return _CHECKPOINTER, _CHECKPOINTER_BACKEND

    try:
        STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(STATE_DB_PATH), check_same_thread=False)
        checkpointer = SqliteSaver(connection)
        checkpointer.setup()
        _CHECKPOINTER = checkpointer
        _CHECKPOINTER_BACKEND = "sqlite"
    except Exception:
        logger.exception("SqliteSaver unavailable; falling back to InMemorySaver")
        _CHECKPOINTER = InMemorySaver()
        _CHECKPOINTER_BACKEND = "memory"

    return _CHECKPOINTER, _CHECKPOINTER_BACKEND


def _get_checkpointed_graph() -> tuple[Any, str]:
    global CHECKPOINTED_AGENT_GRAPH

    checkpointer, backend = _get_checkpointer()
    if CHECKPOINTED_AGENT_GRAPH is None:
        CHECKPOINTED_AGENT_GRAPH = build_graph(checkpointer=checkpointer)
    return CHECKPOINTED_AGENT_GRAPH, backend


def _format_materials(documents: list[dict]) -> str:
    return qa_lc._format_materials(documents)


def _public_references(documents: list[dict]) -> list[dict]:
    return qa_lc._public_references(documents)


def _call_model(prompt, variables: dict, api_key: str) -> str:
    # 🔴 三个参数都必须从 config 读，一个都不能少：
    #    model    —— 端点提供的模型名（第三方端点通常不叫 deepseek-chat）
    #    api_base —— 不传就用官方 api.deepseek.com，第三方 key 必然 401
    #    timeout  —— 统一超时
    #
    #    用 app_config.AI_MODEL（属性查找，发生在调用时）而不是本地常量：
    #    模块级常量在 import 期固化，测试 monkeypatch.setattr(config, ...) 无效。
    model = ChatDeepSeek(
        model=app_config.AI_MODEL,
        api_key=api_key,
        api_base=app_config.AI_BASE_URL,
        timeout=REQUEST_TIMEOUT,
    )
    response = (prompt | model).invoke(variables)
    content = response.content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回内容为空")
    return content.strip()


def _parse_evaluation(raw: str) -> bool:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("评估结果不是合法 JSON")

    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("评估结果不是合法 JSON") from exc

    enough = payload.get("enough")
    if not isinstance(enough, bool):
        raise ValueError("评估结果缺少布尔字段 enough")
    return enough


def _normalize_query(raw: str) -> str:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return ""
    query = lines[0].strip().strip("\"'“”‘’")
    return re.sub(r"\s+", " ", query).strip()


def _unique_query(candidate: str, previous: list[str], question: str) -> str:
    seen = {query.casefold() for query in previous}
    base = candidate or f"{question} 补充检索"
    query = base
    suffix = len(previous) + 1

    while query.casefold() in seen:
        query = f"{base} {suffix}"
        suffix += 1

    return query


def _degraded_update(state: AgentState, reason: str) -> dict[str, Any]:
    return {
        "degraded": True,
        "degradation_reasons": [
            *(state.get("degradation_reasons") or []),
            reason,
        ],
    }


def _route_to(state: AgentState, agent: str) -> dict[str, Any]:
    rounds = state.get("supervisor_rounds", 0) + 1
    route_history = [*(state.get("route_history") or []), agent]
    agent_counts = dict(state.get("agent_counts") or {})
    agent_counts[agent] = agent_counts.get(agent, 0) + 1
    return {
        "next_agent": agent,
        "route_history": route_history,
        "agent_counts": agent_counts,
        "supervisor_rounds": rounds,
    }


def supervisor_node(state: AgentState) -> dict[str, Any]:
    if state.get("supervisor_rounds", 0) >= MAX_ROUNDS:
        return _route_to(state, "answerer")

    last_agent = state.get("last_agent")
    citation_check = state.get("citation_check") or {}

    if not last_agent:
        return _route_to(state, "planner")
    if last_agent == "planner":
        return _route_to(state, "retriever")
    if last_agent == "retriever":
        return _route_to(state, "critic")
    if last_agent == "critic":
        # 🔴 RMB01 改动：只有「可自愈」的不合格才重试。
        #   citation_check 新增了 retryable 字段 —— 「零引用」这种不合格
        #   重试也自愈不了（重试后仍然没有引用），硬重试会走成
        #   answerer→critic→answerer 死循环，直到撞 GRAPH_RECURSION_LIMIT。
        if state.get("answer") and citation_check.get("ok") is False:
            can_retry = citation_check.get("retryable", True)
            if can_retry and state.get("citation_retry_count", 0) <= 1 \
                    and state.get("attempts", 0) < MAX_ATTEMPTS:
                return _route_to(state, "planner")
            return _route_to(state, "answerer")
        if state.get("answer"):
            return _route_to(state, "end")
        if state.get("verdict") == "够":
            return _route_to(state, "answerer")
        if state.get("attempts", 0) < MAX_ATTEMPTS:
            return _route_to(state, "planner")
        return _route_to(state, "answerer")
    if last_agent == "answerer":
        if citation_check.get("ok") is False \
                and citation_check.get("retryable", True) \
                and state.get("citation_retry_count", 0) < 1:
            return _route_to(state, "critic")
        return _route_to(state, "end")

    return _route_to(state, "answerer")


def planner_node(state: AgentState) -> dict[str, Any]:
    question = state["question"].strip()
    previous = state.get("queries") or []

    if not previous:
        return {
            "queries": [question],
            "documents": [],
            "verdict": "",
            "answer": "",
            "citation_check": {},
            "citations": [],
            "invalid_citations": [],
            "attempts": 0,
            "last_agent": "planner",
        }

    api_key = app_config.api_key()
    if not api_key:
        query = _unique_query("", previous, question)
    else:
        raw = _call_model(
            REWRITE_PROMPT,
            {
                "question": question,
                "queries": "\n".join(previous),
                "next_attempt": len(previous) + 1,
            },
            api_key,
        )
        query = _unique_query(_normalize_query(raw), previous, question)

    return {
        "queries": [*previous, query],
        "documents": [],
        "verdict": "",
        "answer": "",
        "citation_check": {},
        "citations": [],
        "invalid_citations": [],
        "last_agent": "planner",
        "prompt_versions": dict(PROMPT_VERSIONS),
    }


def _run_skill_with_retry(name: str, **kwargs: Any) -> tuple[Any, int]:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            return registry.get(name).run(**kwargs), attempt
        except Exception as exc:
            last_error = exc
            logger.exception("Skill %s failed on attempt %s", name, attempt)

    if last_error is None:
        raise RuntimeError(f"Skill {name} failed without an exception")
    raise last_error


def _question_id(document: Any) -> str | None:
    if not isinstance(document, dict):
        return None
    value = document.get("id") or document.get("question_id") or document.get("qid")
    return str(value) if value is not None else None


def _hydrate_documents(
    state: AgentState,
    documents: list[dict],
) -> tuple[list[dict], dict[str, int], list[str]]:
    skill_counts = dict(state.get("skill_counts") or {})
    degradation_reasons: list[str] = []
    hydrated: list[dict] = []

    for document in documents:
        question_id = _question_id(document)
        if question_id is None:
            hydrated.append(document)
            continue

        try:
            detail, attempts = _run_skill_with_retry(
                "get_question",
                question_id=question_id,
            )
            skill_counts["get_question"] = skill_counts.get("get_question", 0) + attempts
            if isinstance(detail, dict):
                hydrated.append({**detail, **document})
            else:
                hydrated.append(document)
        except Exception as exc:
            skill_counts["get_question"] = skill_counts.get("get_question", 0) + 2
            hydrated.append(document)
            degradation_reasons.append(f"get_question failed for {question_id}: {exc}")

    return hydrated, skill_counts, degradation_reasons


def search_documents(query: str, top_k: int = TOP_K) -> list[dict]:
    """按关键词检索**知识库文档**（不是题库）。

    🔴 技能注册名仍是 "search_questions"（skills/search_questions.py），
    这个名字出现在 skill_counts 里被测试断言，改名要连测试一起动 ——
    所以只改这里的函数名，注册名保留。
    """
    skill = registry.get("search_questions")
    return skill.run(query, top_k=top_k, strategy="hybrid")


def retriever_node(state: AgentState) -> dict[str, Any]:
    queries = state.get("queries") or []
    attempts = state.get("attempts", 0)
    skill_counts = dict(state.get("skill_counts") or {})
    if not queries:
        return {
            "documents": [],
            "attempts": attempts,
            "last_agent": "retriever",
            "skill_counts": skill_counts,
        }

    try:
        documents, search_attempts = _run_skill_with_retry(
            "search_questions",
            query=queries[-1],
            top_k=TOP_K,
            strategy="hybrid",
        )
        skill_counts["search_questions"] = (
            skill_counts.get("search_questions", 0) + search_attempts
        )
    except Exception as exc:
        skill_counts["search_questions"] = skill_counts.get("search_questions", 0) + 2
        return {
            "documents": [],
            "attempts": attempts + 1,
            "last_agent": "retriever",
            "skill_counts": skill_counts,
            **_degraded_update(state, f"search_questions failed: {exc}"),
        }

    if not isinstance(documents, list):
        documents = []
    documents = [document for document in documents if isinstance(document, dict)]

    hydrated, skill_counts, hydration_failures = _hydrate_documents(
        {**state, "skill_counts": skill_counts},
        documents,
    )
    update: dict[str, Any] = {
        "documents": hydrated,
        "attempts": attempts + 1,
        "last_agent": "retriever",
        "skill_counts": skill_counts,
    }
    if hydration_failures:
        update.update(
            _degraded_update(
                state,
                "; ".join(hydration_failures),
            )
        )
    return update


def critic_node(state: AgentState) -> dict[str, Any]:
    citation_check = state.get("citation_check") or {}
    if state.get("answer") and citation_check.get("ok") is False:
        return {
            "verdict": "不够",
            "last_agent": "critic",
            "citation_retry_count": state.get("citation_retry_count", 0) + 1,
        }

    documents = state.get("documents") or []
    if not documents:
        return {"verdict": "不够", "last_agent": "critic"}

    api_key = app_config.api_key()
    if not api_key:
        return {"verdict": "不够", "last_agent": "critic"}

    raw = _call_model(
        EVALUATE_PROMPT,
        {
            "question": state["question"],
            "materials": _format_materials(documents),
        },
        api_key,
    )
    return {
        "verdict": "够" if _parse_evaluation(raw) else "不够",
        "last_agent": "critic",
        "prompt_versions": dict(PROMPT_VERSIONS),
    }


def _check_answer_citations(
    state: AgentState,
    answer: str,
    documents: list[dict],
) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
    skill_counts = dict(state.get("skill_counts") or {})
    try:
        citation_check, attempts = _run_skill_with_retry(
            "check_citation",
            answer=answer,
            retrieved_results=documents,
        )
        skill_counts["check_citation"] = skill_counts.get("check_citation", 0) + attempts
        if not isinstance(citation_check, dict):
            raise TypeError("check_citation returned a non-dict result")
        return citation_check, skill_counts, {}
    except Exception as exc:
        skill_counts["check_citation"] = skill_counts.get("check_citation", 0) + 2
        citation_check = {
            "ok": True,
            "citations": [],
            "retrieved_ids": [],
            "invalid_citations": [],
            "check_failed": True,
            "error": str(exc),
        }
        return citation_check, skill_counts, _degraded_update(
            state,
            f"check_citation failed: {exc}",
        )


def answerer_node(state: AgentState) -> dict[str, Any]:
    documents = state.get("documents") or []
    verdict = state.get("verdict")

    if not documents:
        answer = "知识库里没有检索到相关内容，换个问法或先上传文档"
    else:
        api_key = app_config.api_key()
        if not api_key:
            answer = "服务未配置 API 密钥，暂时无法回答"
        else:
            answer = _call_model(
                qa_lc.PROMPT,
                {
                    "question": state["question"],
                    "materials": _format_materials(documents),
                },
                api_key,
            )
            if verdict == "不够":
                answer = f"现有材料不足以完整回答该问题。以下仅基于现有材料回答：\n{answer}"

    citation_check, skill_counts, degradation_update = _check_answer_citations(
        state,
        answer,
        documents,
    )
    citation_history = [
        *(state.get("citation_history") or []),
        citation_check,
    ]

    return {
        "answer": answer,
        "last_agent": "answerer",
        "prompt_versions": dict(PROMPT_VERSIONS),
        "citation_check": citation_check,
        "citation_history": citation_history,
        "citations": citation_check.get("citations") or [],
        "invalid_citations": citation_check.get("invalid_citations") or [],
        "skill_counts": skill_counts,
        **degradation_update,
    }


def _safe_node(name: str, node):
    def wrapped(state: AgentState) -> dict[str, Any]:
        try:
            return node(state)
        except Exception as exc:
            logger.exception("Agent %s failed", name)
            degradation_update = _degraded_update(state, f"{name} failed: {exc}")
            if name == "planner":
                queries = state.get("queries") or [state.get("question", "")]
                return {
                    "queries": queries,
                    "documents": [],
                    "verdict": "",
                    "answer": "",
                    "last_agent": "planner",
                    **degradation_update,
                }
            if name == "retriever":
                return {
                    "documents": [],
                    "attempts": state.get("attempts", 0) + 1,
                    "last_agent": "retriever",
                    **degradation_update,
                }
            if name == "critic":
                return {
                    "verdict": "不够",
                    "last_agent": "critic",
                    **degradation_update,
                }
            if name == "answerer":
                return {
                    "answer": "回答生成失败，已返回检索到的材料",
                    "last_agent": "answerer",
                    "citation_check": {
                        "ok": True,
                        "citations": [],
                        "invalid_citations": [],
                        "check_failed": True,
                    },
                    "citations": [],
                    "invalid_citations": [],
                    **degradation_update,
                }
            return degradation_update

    return wrapped


def route_after_supervisor(state: AgentState) -> str:
    return state.get("next_agent") or "end"


def build_graph(checkpointer: Any = None):
    graph = StateGraph(AgentState)
    graph.add_node("supervisor", _safe_node("supervisor", supervisor_node))
    graph.add_node("planner", _safe_node("planner", planner_node))
    graph.add_node("retriever", _safe_node("retriever", retriever_node))
    graph.add_node("critic", _safe_node("critic", critic_node))
    graph.add_node("answerer", _safe_node("answerer", answerer_node))

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "planner": "planner",
            "retriever": "retriever",
            "critic": "critic",
            "answerer": "answerer",
            "end": END,
        },
    )
    graph.add_edge("planner", "supervisor")
    graph.add_edge("retriever", "supervisor")
    graph.add_edge("critic", "supervisor")
    graph.add_edge("answerer", "supervisor")

    if checkpointer is None:
        return graph.compile()
    return graph.compile(checkpointer=checkpointer)


AGENT_GRAPH = build_graph()


def _empty_result(
    answer: str,
    *,
    thread_id: str | None = None,
    backend: str = "none",
    degraded: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """构造一个「没有真正跑图」的返回值。

    🔴 **RMB01 改动**：`degraded` 与 `degradation_reasons` 从硬编码 False/[]
    改成由调用点显式声明。

    原来一律返回 degraded=False，于是「没配 key 所以没能调模型」这种
    **实质降级**也会被标成"一切正常"。后果不只是显示问题 ——
    阶段 4 的额度扣减要按 degraded 判断该不该计费，标记错了就**算错账**。

    所以现在每个早退点都必须说清自己是不是降级：
      · 空问题      → 不是降级（是参数错误，压根没开始）
      · 没配key     → **是降级**（应该调模型而没调成）
      · 兜底异常    → **是降级**
    """

    return {
        "answer": answer,
        "references": [],
        "queries_tried": [],
        "attempts": 0,
        "route_history": [],
        "agent_counts": {},
        "skill_counts": {},
        "supervisor_rounds": 0,
        "degraded": degraded,
        "degradation_reasons": [reason] if reason else [],
        "prompt_versions": dict(PROMPT_VERSIONS),
        "citations": [],
        "invalid_citations": [],
        "citation_check": {},
        "citation_history": [],
        "citation_retry_count": 0,
        "checkpoint_thread_id": thread_id,
        "checkpointer": backend,
        "conversation_history": [],
    }


def run_agent(question: str, thread_id: str | None = None) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        # 参数错误，不算降级 —— 压根没开始跑
        return _empty_result("问题不能为空", thread_id=thread_id)

    normalized_question = question.strip()
    if not app_config.api_key():
        # 🔴 实质降级：应该调模型但没有 key，必须如实标记
        return _empty_result(
            "服务未配置 API 密钥，暂时无法回答",
            thread_id=thread_id,
            degraded=True,
            reason="未配置 API 密钥，未调用模型",
        )

    graph = AGENT_GRAPH
    backend = "none"
    conversation_history = [normalized_question]
    # ⚠️ 这里的 config 是 **LangGraph 的运行配置字典**，与 app_config 模块无关。
    #    两者同名很容易混，所以配置模块统一用 app_config 别名导入。
    config: dict[str, Any] = {"recursion_limit": 40}

    if thread_id:
        graph, backend = _get_checkpointed_graph()
        config = {
            "recursion_limit": 40,
            "configurable": {"thread_id": thread_id},
        }
        previous_state = graph.get_state(config)
        previous_values = previous_state.values if previous_state else {}
        conversation_history = [
            *(previous_values.get("conversation_history") or []),
            normalized_question,
        ]

    initial_state: AgentState = {
        "question": normalized_question,
        "thread_id": thread_id or "",
        "conversation_history": conversation_history,
        "queries": [],
        "documents": [],
        "verdict": "",
        "answer": "",
        "attempts": 0,
        "last_agent": "",
        "next_agent": "",
        "route_history": [],
        "agent_counts": {},
        "skill_counts": {},
        "supervisor_rounds": 0,
        "degraded": False,
        "degradation_reasons": [],
        "prompt_versions": dict(PROMPT_VERSIONS),
        "citation_check": {},
        "citation_history": [],
        "citations": [],
        "invalid_citations": [],
        "citation_retry_count": 0,
    }

    try:
        final_state = graph.invoke(initial_state, config=config)
    except Exception as exc:
        logger.exception("Multi-Agent graph failed")
        # 🔴 图崩了也是降级 —— 走统一的参数入口，不要事后手动改字段
        return _empty_result(
            "Agent 处理失败，请稍后重试",
            thread_id=thread_id,
            backend=backend,
            degraded=True,
            reason=f"graph failed: {exc}",
        )

    queries = list(dict.fromkeys(final_state.get("queries") or []))
    documents = final_state.get("documents") or []
    return {
        "answer": final_state.get("answer") or "知识库里没有检索到相关内容",
        "references": _public_references(documents),
        "queries_tried": queries,
        "attempts": final_state.get("attempts", 0),
        "route_history": final_state.get("route_history") or [],
        "agent_counts": final_state.get("agent_counts") or {},
        "skill_counts": final_state.get("skill_counts") or {},
        "supervisor_rounds": final_state.get("supervisor_rounds", 0),
        "degraded": bool(final_state.get("degraded", False)),
        "degradation_reasons": final_state.get("degradation_reasons") or [],
        "prompt_versions": final_state.get("prompt_versions") or dict(PROMPT_VERSIONS),
        "citations": final_state.get("citations") or [],
        "invalid_citations": final_state.get("invalid_citations") or [],
        "citation_check": final_state.get("citation_check") or {},
        "citation_history": final_state.get("citation_history") or [],
        "citation_retry_count": final_state.get("citation_retry_count", 0),
        "checkpoint_thread_id": thread_id,
        "checkpointer": backend,
        "conversation_history": final_state.get("conversation_history") or conversation_history,
    }
