import requests

import qa


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


def test_build_prompt_contains_reference_stem_and_answer():
    prompt = qa.build_prompt("依赖注入有哪些方式？", [REFERENCE])

    assert REFERENCE["stem"] in prompt
    assert REFERENCE["answer"] in prompt


def test_build_prompt_contains_reference_options():
    prompt = qa.build_prompt("依赖注入有哪些方式？", [REFERENCE])

    assert "A. 构造器注入" in prompt


def test_empty_search_does_not_call_api(monkeypatch):
    monkeypatch.setattr(qa.retriever, "search", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        qa.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("requests.post should not be called")
        ),
    )

    result = qa.ask("依赖注入有哪些方式？")

    assert result == {"answer": "没有找到相关题目材料", "references": []}


def test_missing_api_key_does_not_call_api(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(qa.retriever, "search", lambda *args, **kwargs: [REFERENCE])
    monkeypatch.setattr(
        qa.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("requests.post should not be called")
        ),
    )

    result = qa.ask("依赖注入有哪些方式？")

    assert result["answer"] == "服务未配置 API 密钥，暂时无法回答"
    assert result["references"] == [
        {
            "id": REFERENCE["id"],
            "stem": REFERENCE["stem"],
            "answer": REFERENCE["answer"],
        }
    ]


def test_api_timeout_returns_friendly_message(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(qa.retriever, "search", lambda *args, **kwargs: [REFERENCE])

    def timeout_post(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(qa.requests, "post", timeout_post)

    result = qa.ask("依赖注入有哪些方式？")

    assert result["answer"] == "请求大模型服务超时，请稍后重试"


def test_api_429_returns_friendly_message(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(qa.retriever, "search", lambda *args, **kwargs: [REFERENCE])

    class FakeResponse:
        status_code = 429

        def raise_for_status(self):
            raise requests.HTTPError(response=self)

    monkeypatch.setattr(qa.requests, "post", lambda *args, **kwargs: FakeResponse())

    result = qa.ask("依赖注入有哪些方式？")

    assert result["answer"] == "大模型服务返回错误（HTTP 429），请稍后重试"


def test_successful_response_references_only_include_public_fields(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(qa.retriever, "search", lambda *args, **kwargs: [REFERENCE])

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "构造器注入和 Setter 注入。"}}]}

    monkeypatch.setattr(qa.requests, "post", lambda *args, **kwargs: FakeResponse())

    result = qa.ask("依赖注入有哪些方式？")

    assert result["answer"] == "构造器注入和 Setter 注入。"
    assert result["references"] == [
        {
            "id": REFERENCE["id"],
            "stem": REFERENCE["stem"],
            "answer": REFERENCE["answer"],
        }
    ]
    assert all(set(reference) == {"id", "stem", "answer"} for reference in result["references"])
    assert all("score" not in reference for reference in result["references"])
