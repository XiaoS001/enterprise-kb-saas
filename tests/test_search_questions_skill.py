from skills.search_questions import create_search_questions_skill


def test_search_questions_skill_uses_retriever_and_returns_results() -> None:
    calls = []

    def retriever(query: str, *, top_k: int, strategy: str):
        calls.append((query, top_k, strategy))
        return [{"id": "1", "text": "first"}]

    skill = create_search_questions_skill(retriever)

    assert skill.name == "search_questions"
    assert skill.run("binary tree", top_k=3, strategy="keyword") == [
        {"id": "1", "text": "first"}
    ]
    assert calls == [("binary tree", 3, "keyword")]


def test_search_questions_skill_rejects_invalid_strategy() -> None:
    skill = create_search_questions_skill(lambda query, **kwargs: [])

    try:
        skill.run("binary tree", strategy="invalid")
    except ValueError as exc:
        assert "strategy" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
