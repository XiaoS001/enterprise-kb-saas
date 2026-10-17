import json

import numpy as np
import pytest

import retriever_vec


def _vector(x: float, y: float) -> np.ndarray:
    vector = np.zeros(retriever_vec.EMBEDDING_DIM, dtype=np.float32)
    vector[0] = x
    vector[1] = y
    return vector


def _question(question_id: int, stem: str) -> dict:
    return {
        "id": question_id,
        "type": "fill",
        "stem": stem,
        "options": [],
        "answer": "答案",
        "explanation": "解析",
    }


class FakeEmbeddingModel:
    def __init__(self, document_vectors, query_vector):
        self.document_vectors = document_vectors
        self.query_vector = query_vector
        self.embed_calls = 0
        self.query_calls = 0

    def embed(self, texts):
        self.embed_calls += 1
        for vector in self.document_vectors:
            yield np.asarray(vector, dtype=np.float32)

    def query_embed(self, queries):
        self.query_calls += 1
        for _ in queries:
            yield np.asarray(self.query_vector, dtype=np.float32)


@pytest.fixture
def vector_env(tmp_path, monkeypatch):
    questions = [
        _question(1, "严格匹配的问题"),
        _question(2, "部分相关的问题"),
        _question(3, "无关的问题"),
    ]
    questions_path = tmp_path / "questions.json"
    cache_path = tmp_path / "question_vectors.npz"
    questions_path.write_text(
        json.dumps(questions, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(retriever_vec, "QUESTIONS_PATH", questions_path)
    monkeypatch.setattr(retriever_vec, "CACHE_PATH", cache_path)
    monkeypatch.setattr(retriever_vec, "_questions", [])
    monkeypatch.setattr(retriever_vec, "_vectors", None)
    monkeypatch.setattr(retriever_vec, "_index_ready", False)
    monkeypatch.setattr(retriever_vec, "_model", None)

    model = FakeEmbeddingModel(
        document_vectors=[
            _vector(1.0, 0.0),
            _vector(0.8, 0.6),
            _vector(0.0, 1.0),
        ],
        query_vector=_vector(1.0, 0.0),
    )
    monkeypatch.setattr(retriever_vec, "_get_model", lambda: model)

    return questions, model, cache_path


def test_search_returns_aligned_fields_and_minimum_count(vector_env):
    questions, _, _ = vector_env

    results = retriever_vec.search("测试问题", top_k=10)

    assert len(results) == len(questions)
    assert set(results[0]) == {
        "id",
        "type",
        "stem",
        "options",
        "answer",
        "explanation",
        "score",
    }


def test_empty_and_whitespace_queries_return_empty(vector_env):
    _, model, _ = vector_env

    assert retriever_vec.search("") == []
    assert retriever_vec.search("   ") == []
    assert model.embed_calls == 0
    assert model.query_calls == 0


def test_top_k_boundaries(vector_env):
    assert retriever_vec.search("测试问题", top_k=0) == []
    assert len(retriever_vec.search("测试问题", top_k=1)) == 1
    assert len(retriever_vec.search("测试问题", top_k=10)) == 3


def test_cosine_similarity_ordering(vector_env):
    results = retriever_vec.search("测试问题", top_k=3)

    assert [result["id"] for result in results] == [1, 2, 3]
    assert results[0]["score"] > results[1]["score"] > results[2]["score"]


def test_search_auto_builds_index(vector_env):
    _, model, cache_path = vector_env

    assert retriever_vec._index_ready is False
    results = retriever_vec.search("测试问题", top_k=1)

    assert results[0]["id"] == 1
    assert retriever_vec._index_ready is True
    assert model.embed_calls == 1
    assert cache_path.is_file()


def test_corrupted_cache_is_rebuilt(vector_env):
    _, model, cache_path = vector_env
    retriever_vec.build_index()
    cache_path.write_bytes(b"not an npz file")

    retriever_vec._index_ready = False
    retriever_vec._vectors = None
    assert retriever_vec.build_index() == 3

    assert model.embed_calls == 2
    assert np.load(cache_path, allow_pickle=False)["vectors"].shape == (
        3,
        retriever_vec.EMBEDDING_DIM,
    )


def test_dimension_mismatch_in_cache_is_rebuilt(vector_env):
    questions, model, cache_path = vector_env
    np.savez_compressed(
        cache_path,
        model_name=np.asarray(retriever_vec.MODEL_NAME),
        dimension=np.asarray(2, dtype=np.int32),
        questions_hash=np.asarray(retriever_vec._questions_hash(questions)),
        ids_json=np.asarray(json.dumps([1, 2, 3])),
        vectors=np.zeros((3, 2), dtype=np.float32),
    )

    assert retriever_vec.build_index() == 3

    assert model.embed_calls == 1
    assert retriever_vec._vectors.shape == (3, retriever_vec.EMBEDDING_DIM)


def test_vector_search_does_not_require_api_key(vector_env, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    results = retriever_vec.search("测试问题", top_k=2)

    assert len(results) == 2
    assert all("score" in result for result in results)
