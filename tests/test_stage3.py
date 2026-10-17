import json
import uuid

import agent
import app as app_module
import qa_lc
from skills import Skill


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


def _mock_search(monkeypatch):
    monkeypatch.setattr(
        agent.retriever_lc,
        "search",
        lambda query, top_k=5: [REFERENCE],
    )


def _mock_get_question(monkeypatch, calls):
    def fake_get_question(question_id):
        calls.append(question_id)
        return REFERENCE

    skill = Skill(
        name="get_question",
        description="test get_question",
        run=fake_get_question,
        input_schema={},
    )
    monkeypatch.setitem(agent.registry._skills, "get_question", skill)


def _mock_model(monkeypatch, answer="[Q190]"):
    def fake_model(prompt, variables, api_key):
        assert api_key == "test-key"
        if prompt is agent.EVALUATE_PROMPT:
            return json.dumps({"enough": True, "reason": "足够"})
        if prompt is agent.REWRITE_PROMPT:
            return "补充查询"
        if prompt is qa_lc.PROMPT:
            return answer(variables) if callable(answer) else answer
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(agent, "_call_model", fake_model)


def test_agent_flow_calls_get_question_and_check_citation(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _mock_search(monkeypatch)
    _mock_model(monkeypatch, "[Q190]")
    get_question_calls = []
    _mock_get_question(monkeypatch, get_question_calls)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert get_question_calls == ["190"]
    assert result["skill_counts"]["get_question"] == 1
    assert result["skill_counts"]["check_citation"] == 1
    assert result["citation_check"]["ok"] is True
    assert result["citations"] == ["190"]
    assert result["invalid_citations"] == []


def test_invalid_citation_is_checked_and_triggers_one_retry(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _mock_search(monkeypatch)
    answers = iter(["[Q999]", "[Q190]"])
    _mock_model(monkeypatch, lambda variables: next(answers))
    get_question_calls = []
    _mock_get_question(monkeypatch, get_question_calls)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert result["citation_history"][0]["invalid_citations"] == ["999"]
    assert result["citation_retry_count"] == 1
    assert result["citation_check"]["ok"] is True
    assert result["invalid_citations"] == []
    assert result["route_history"] == [
        "planner",
        "retriever",
        "critic",
        "answerer",
        "critic",
        "planner",
        "retriever",
        "critic",
        "answerer",
        "end",
    ]


def test_single_agent_failure_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _mock_search(monkeypatch)
    get_question_calls = []
    _mock_get_question(monkeypatch, get_question_calls)

    def fake_model(prompt, variables, api_key):
        if prompt is agent.EVALUATE_PROMPT:
            raise RuntimeError("critic unavailable")
        if prompt is agent.REWRITE_PROMPT:
            return "补充查询"
        if prompt is qa_lc.PROMPT:
            return "降级后的答案"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(agent, "_call_model", fake_model)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert result["answer"].startswith("现有材料不足以完整回答该问题。")
    assert result["degraded"] is True
    assert any("critic failed" in reason for reason in result["degradation_reasons"])


def test_skill_failure_retries_once_then_continues(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    search_calls = []
    _mock_model(monkeypatch, "[Q190]")
    get_question_calls = []
    _mock_get_question(monkeypatch, get_question_calls)

    def flaky_search(query, top_k=5):
        search_calls.append(query)
        if len(search_calls) == 1:
            raise RuntimeError("temporary search failure")
        return [REFERENCE]

    monkeypatch.setattr(agent.retriever_lc, "search", flaky_search)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert len(search_calls) == 2
    assert result["skill_counts"]["search_questions"] == 2
    assert result["degraded"] is False


def test_sqlite_checkpointer_restores_same_thread(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _mock_search(monkeypatch)
    _mock_model(monkeypatch, "[Q190]")
    get_question_calls = []
    _mock_get_question(monkeypatch, get_question_calls)
    thread_id = f"stage3-{uuid.uuid4().hex}"

    first = agent.run_agent("第一个问题", thread_id=thread_id)
    second = agent.run_agent("第二个问题", thread_id=thread_id)

    assert first["checkpointer"] == "sqlite"
    assert first["conversation_history"] == ["第一个问题"]
    assert second["conversation_history"] == ["第一个问题", "第二个问题"]


def test_prompt_versions_are_returned(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _mock_search(monkeypatch)
    _mock_model(monkeypatch, "[Q190]")
    get_question_calls = []
    _mock_get_question(monkeypatch, get_question_calls)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert result["prompt_versions"] == {
        "planner": "v1",
        "critic": "v1",
        "answerer": "qa_lc.v1",
    }


def test_agent_endpoint_forwards_thread_id(client, monkeypatch):
    calls = []

    def fake_run_agent(question, thread_id=None):
        calls.append((question, thread_id))
        return {
            "answer": "测试回答",
            "references": [],
            "checkpoint_thread_id": thread_id,
        }

    monkeypatch.setattr(app_module, "_run_agent", fake_run_agent)

    response = client.post(
        "/api/agent/ask",
        json={"question": "测试问题", "thread_id": "thread-1"},
    )

    assert response.status_code == 200
    assert calls == [("测试问题", "thread-1")]
    assert response.get_json()["checkpoint_thread_id"] == "thread-1"
