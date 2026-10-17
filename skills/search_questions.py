from __future__ import annotations

import inspect
from typing import Any

from skills.registry import Skill

SEARCH_QUESTIONS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "top_k": {"type": "integer", "minimum": 1, "default": 5},
        "strategy": {
            "type": "string",
            "enum": ["keyword", "semantic", "hybrid"],
            "default": "hybrid",
        },
    },
    "required": ["query"],
}

_default_retriever: Any = None


def set_default_retriever(retriever: Any) -> None:
    global _default_retriever
    _default_retriever = retriever


def _accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return True

    return any(
        parameter.name == keyword
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _invoke_retriever(
    retriever: Any,
    query: str,
    top_k: int,
    strategy: str,
) -> list[Any]:
    if hasattr(retriever, "search") and callable(retriever.search):
        target = retriever.search
    elif callable(retriever):
        target = retriever
    else:
        raise TypeError("retriever must be callable or expose a callable search method")

    kwargs = {}
    if _accepts_keyword(target, "top_k"):
        kwargs["top_k"] = top_k
    if _accepts_keyword(target, "strategy"):
        kwargs["strategy"] = strategy

    return list(target(query, **kwargs))


def search_questions(
    query: str,
    top_k: int = 5,
    strategy: str = "hybrid",
    *,
    retriever: Any = None,
) -> list[Any]:
    if not query.strip():
        raise ValueError("query must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if strategy not in {"keyword", "semantic", "hybrid"}:
        raise ValueError("strategy must be one of: keyword, semantic, hybrid")

    active_retriever = retriever if retriever is not None else _default_retriever
    if active_retriever is None:
        raise RuntimeError("retriever is required to run search_questions")

    return _invoke_retriever(active_retriever, query, top_k, strategy)


def create_search_questions_skill(retriever: Any = None) -> Skill:
    def run(
        query: str,
        top_k: int = 5,
        strategy: str = "hybrid",
    ) -> list[Any]:
        return search_questions(
            query,
            top_k=top_k,
            strategy=strategy,
            retriever=retriever,
        )

    return Skill(
        name="search_questions",
        description="Search the question bank with keyword, semantic, or hybrid retrieval.",
        run=run,
        input_schema=SEARCH_QUESTIONS_INPUT_SCHEMA,
    )


skill = create_search_questions_skill()
