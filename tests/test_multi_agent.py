import json

import agent
import qa_lc
from skills.registry import registry

REFERENCE = {
    "id": 190,
    "type": "choice",
    "stem": "Spring 依赖注入有哪些方式？",
    "options": [
        {"letter": "A", "text": "构造器注入"},
        {"letter": "B", "text": "Setter 注入"},
    ],
    "answer": "构造器注入、Setter 注入",
    "explanation": "常见的依赖注入方式。",
    "score": 12.3,
}


def test_default_skills_are_registered() -> None:
    assert [skill.name for skill in registry.list_all()] == [
        "check_citation",
        "get_question",
        "search_questions",
    ]


def test_supervisor_routes_from_shared_state() -> None:
    assert agent.supervisor_node({"supervisor_rounds": 0})["next_agent"] == "planner"
    assert (
        agent.supervisor_node({"last_agent": "planner", "supervisor_rounds": 1})[
            "next_agent"
        ]
        == "retriever"
    )
    assert (
        agent.supervisor_node({"last_agent": "retriever", "supervisor_rounds": 2})[
            "next_agent"
        ]
        == "critic"
    )
    assert (
        agent.supervisor_node(
            {
                "last_agent": "critic",
                "verdict": "够",
                "attempts": 1,
                "supervisor_rounds": 3,
            }
        )["next_agent"]
        == "answerer"
    )
    assert (
        agent.supervisor_node(
            {
                "last_agent": "critic",
                "verdict": "不够",
                "attempts": 1,
                "supervisor_rounds": 3,
            }
        )["next_agent"]
        == "planner"
    )
    assert (
        agent.supervisor_node(
            {
                "last_agent": "critic",
                "verdict": "不够",
                "attempts": agent.MAX_ATTEMPTS,
                "supervisor_rounds": 3,
            }
        )["next_agent"]
        == "answerer"
    )


def test_always_unsatisfied_critic_returns_after_attempt_limit(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        agent.retriever_lc,
        "search",
        lambda query, top_k=5: [REFERENCE],
    )

    def fake_model(prompt, variables, api_key):
        assert api_key == "test-key"
        if prompt is agent.EVALUATE_PROMPT:
            return json.dumps({"enough": False, "reason": "材料仍不足"})
        if prompt is agent.REWRITE_PROMPT:
            return f"补充查询 {variables['next_attempt']}"
        if prompt is qa_lc.PROMPT:
            return "最终答案"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(agent, "_call_model", fake_model)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert result["attempts"] == agent.MAX_ATTEMPTS
    assert result["answer"].startswith("现有材料不足以完整回答该问题。")
    assert result["route_history"] == [
        "planner",
        "retriever",
        "critic",
        "planner",
        "retriever",
        "critic",
        "planner",
        "retriever",
        "critic",
        "answerer",
        "end",
    ]
    assert result["agent_counts"] == {
        "planner": 3,
        "retriever": 3,
        "critic": 3,
        "answerer": 1,
        "end": 1,
    }
