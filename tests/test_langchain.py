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


def test_langchain_empty_search_does_not_call_model(monkeypatch):
    monkeypatch.setattr(qa_lc.retriever_lc, "search", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        qa_lc,
        "_invoke_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("model should not be called")
        ),
    )

    result = qa_lc.ask("依赖注入有哪些方式？")

    assert result == {"answer": "没有找到相关题目材料", "references": []}


def test_langchain_missing_api_key_returns_friendly_message(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        qa_lc.retriever_lc,
        "search",
        lambda *args, **kwargs: [REFERENCE],
    )
    monkeypatch.setattr(
        qa_lc,
        "_invoke_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("model should not be called")
        ),
    )

    result = qa_lc.ask("依赖注入有哪些方式？")

    assert result["answer"] == "服务未配置 API 密钥，暂时无法回答"
    assert result["references"] == [
        {
            "id": REFERENCE["id"],
            "stem": REFERENCE["stem"],
            "answer": REFERENCE["answer"],
        }
    ]


def test_langchain_references_only_include_public_fields(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        qa_lc.retriever_lc,
        "search",
        lambda *args, **kwargs: [REFERENCE],
    )
    monkeypatch.setattr(qa_lc, "_invoke_model", lambda *args, **kwargs: "测试回答")

    result = qa_lc.ask("依赖注入有哪些方式？")

    assert result["answer"] == "测试回答"
    assert result["references"] == [
        {
            "id": REFERENCE["id"],
            "stem": REFERENCE["stem"],
            "answer": REFERENCE["answer"],
        }
    ]
    assert all(set(reference) == {"id", "stem", "answer"} for reference in result["references"])
    assert all("score" not in reference for reference in result["references"])


def test_app_uses_langchain_when_enabled(client, monkeypatch):
    monkeypatch.setenv("USE_LANGCHAIN", "1")
    monkeypatch.setattr(
        app_module.qa,
        "ask",
        lambda question: (_ for _ in ()).throw(
            AssertionError("old qa should not be called")
        ),
    )
    monkeypatch.setattr(
        qa_lc,
        "ask",
        lambda question: {"answer": "LangChain 回答", "references": []},
    )

    response = client.post("/api/ask", json={"question": "测试问题"})

    assert response.status_code == 200
    assert response.get_json() == {"answer": "LangChain 回答", "references": []}
