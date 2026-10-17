import retriever


def test_search_returns_relevant_result():
    results = retriever.search("依赖注入")

    assert results
    assert "依赖注入" in results[0]["stem"]


def test_search_empty_query_returns_empty_list():
    assert retriever.search("") == []


def test_search_unrelated_query_returns_empty_list():
    assert retriever.search("今天天气怎么样") == []


def test_search_respects_top_k():
    results = retriever.search("依赖注入", top_k=2)

    assert len(results) == 2
