import app as app_module


def test_ask_requires_question(client):
    response = client.post("/api/ask", json={})

    assert response.status_code == 400
    assert response.get_json() == {"error": "问题不能为空"}


def test_ask_rejects_question_longer_than_500_characters(client):
    response = client.post("/api/ask", json={"question": "问" * 501})

    assert response.status_code == 400
    assert response.get_json() == {"error": "问题过长"}


def test_ask_returns_qa_result(client, monkeypatch):
    monkeypatch.setattr(
        app_module.qa,
        "ask",
        lambda question: {"answer": "测试回答", "references": []},
    )

    response = client.post("/api/ask", json={"question": "测试问题"})

    assert response.status_code == 200
    assert response.get_json() == {"answer": "测试回答", "references": []}
