import json

import agent
import app as app_module
import qa_lc


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


def _set_api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


def _mock_search(monkeypatch, calls):
    def fake_search(query, top_k=5):
        calls.append(query)
        return [REFERENCE]

    monkeypatch.setattr(agent.retriever_lc, "search", fake_search)


def test_insufficient_material_triggers_retry(monkeypatch):
    _set_api_key(monkeypatch)
    search_calls = []
    _mock_search(monkeypatch, search_calls)
    evaluations = iter([False, True])

    def fake_model(prompt, variables, api_key):
        assert api_key == "test-key"
        if prompt is agent.EVALUATE_PROMPT:
            return json.dumps(
                {"enough": next(evaluations), "reason": "测试判断"},
                ensure_ascii=False,
            )
        if prompt is agent.REWRITE_PROMPT:
            return "Spring 依赖注入的几种实现方式"
        if prompt is qa_lc.PROMPT:
            return "构造器注入和 Setter 注入。"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(agent, "_call_model", fake_model)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert len(search_calls) == 2
    assert result["attempts"] == 2
    assert result["queries_tried"] == [
        "Spring 依赖注入有哪些方式？",
        "Spring 依赖注入的几种实现方式",
    ]
    assert result["answer"] == "构造器注入和 Setter 注入。"


def test_retry_stops_at_attempt_limit(monkeypatch):
    _set_api_key(monkeypatch)
    search_calls = []
    _mock_search(monkeypatch, search_calls)
    rewrite_calls = 0
    generate_calls = 0

    def fake_model(prompt, variables, api_key):
        nonlocal rewrite_calls, generate_calls
        assert api_key == "test-key"
        if prompt is agent.EVALUATE_PROMPT:
            return '{"enough": false, "reason": "材料仍不完整"}'
        if prompt is agent.REWRITE_PROMPT:
            rewrite_calls += 1
            return f"补充查询 {rewrite_calls}"
        if prompt is qa_lc.PROMPT:
            generate_calls += 1
            return "仅能找到部分信息。"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(agent, "_call_model", fake_model)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert len(search_calls) == agent.MAX_ATTEMPTS
    assert result["attempts"] == agent.MAX_ATTEMPTS
    assert len(result["queries_tried"]) == agent.MAX_ATTEMPTS
    assert rewrite_calls == agent.MAX_ATTEMPTS - 1
    assert generate_calls == 1
    assert result["answer"].startswith("现有材料不足以完整回答该问题。")


def test_rewritten_queries_are_unique(monkeypatch):
    _set_api_key(monkeypatch)
    search_calls = []
    _mock_search(monkeypatch, search_calls)

    def fake_model(prompt, variables, api_key):
        if prompt is agent.EVALUATE_PROMPT:
            return '{"enough": false, "reason": "材料不足"}'
        if prompt is agent.REWRITE_PROMPT:
            return "Spring 依赖注入有哪些方式？"
        if prompt is qa_lc.PROMPT:
            return "材料中的信息有限。"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(agent, "_call_model", fake_model)

    result = agent.run_agent("Spring 依赖注入有哪些方式？")

    assert len(result["queries_tried"]) == agent.MAX_ATTEMPTS
    assert len(result["queries_tried"]) == len(set(result["queries_tried"]))


def test_agent_endpoint_is_available(client, monkeypatch):
    expected = {
        "answer": "Agent 回答",
        "references": [],
        "queries_tried": ["测试问题"],
        "attempts": 1,
    }
    monkeypatch.setattr(app_module, "_run_agent", lambda question: expected)

    response = client.post("/api/agent/ask", json={"question": "测试问题"})

    assert response.status_code == 200
    assert response.get_json() == expected


def test_ask_uses_agent_when_enabled(client, monkeypatch):
    monkeypatch.setenv("USE_AGENT", "1")
    expected = {
        "answer": "Agent 回答",
        "references": [],
        "queries_tried": ["测试问题"],
        "attempts": 1,
    }
    monkeypatch.setattr(app_module, "_run_agent", lambda question: expected)
    monkeypatch.setattr(
        app_module.qa,
        "ask",
        lambda question: (_ for _ in ()).throw(
            AssertionError("old qa should not be called")
        ),
    )

    response = client.post("/api/ask", json={"question": "测试问题"})

    assert response.status_code == 200
    assert response.get_json() == expected
