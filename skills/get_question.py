from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skills.registry import Skill

GET_QUESTION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "question_id": {"type": "string"},
    },
    "required": ["question_id"],
}

_default_source: Any = None


def set_default_question_source(source: Any) -> None:
    global _default_source
    _default_source = source


def get_question(question_id: str, *, source: Any = None) -> Any:
    if not question_id:
        raise ValueError("question_id must not be empty")

    active_source = source if source is not None else _default_source
    if active_source is None:
        raise RuntimeError("source is required to run get_question")

    if hasattr(active_source, "get_question") and callable(active_source.get_question):
        return active_source.get_question(question_id)
    if hasattr(active_source, "get_by_id") and callable(active_source.get_by_id):
        return active_source.get_by_id(question_id)
    if isinstance(active_source, Mapping):
        return active_source[question_id]

    questions_by_id = getattr(active_source, "_questions_by_id", None)
    if isinstance(questions_by_id, Mapping):
        if not questions_by_id and hasattr(active_source, "build_index"):
            active_source.build_index()
            questions_by_id = getattr(active_source, "_questions_by_id", {})
        return questions_by_id[question_id]

    if callable(active_source):
        return active_source(question_id)

    raise TypeError(
        "source must be a mapping, callable, or expose get_question/get_by_id"
    )


def create_get_question_skill(source: Any = None) -> Skill:
    def run(question_id: str) -> Any:
        return get_question(question_id, source=source)

    return Skill(
        name="get_question",
        description="Fetch one question's full details by its question id.",
        run=run,
        input_schema=GET_QUESTION_INPUT_SCHEMA,
    )


skill = create_get_question_skill()
