import hashlib
import json
import os
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIM = 512
HF_ENDPOINT = "https://hf-mirror.com"

BASE_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = BASE_DIR / "data" / "questions.json"
CACHE_PATH = BASE_DIR / "data" / "question_vectors.npz"

_questions: list[dict] = []
_vectors: np.ndarray | None = None
_index_ready = False
_model: TextEmbedding | None = None


def _normalize_options(options) -> list[dict]:
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            return []
    if not isinstance(options, list):
        return []
    return [option for option in options if isinstance(option, dict)]


def _question_text(question: dict) -> str:
    stem = str(question.get("stem") or "")
    options = _normalize_options(question.get("options"))
    options_text = " ".join(
        f"{option.get('letter') or ''} {option.get('text') or ''}"
        for option in options
    )
    answer = str(question.get("answer") or "")
    explanation = str(question.get("explanation") or "")
    return f"{stem} {options_text} {answer} {explanation}"


def _load_questions() -> list[dict]:
    if not QUESTIONS_PATH.is_file():
        raise FileNotFoundError(f"题库数据文件不存在: {QUESTIONS_PATH}")

    try:
        with QUESTIONS_PATH.open("r", encoding="utf-8") as file:
            questions = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"题库数据文件格式错误: {QUESTIONS_PATH}") from exc

    if not isinstance(questions, list):
        raise ValueError(f"题库数据文件应为 JSON 数组: {QUESTIONS_PATH}")

    return questions


def _questions_hash(questions: list[dict]) -> str:
    payload = json.dumps(
        questions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _question_ids(questions: list[dict]) -> list:
    return [question.get("id") for question in questions]


def _get_model() -> TextEmbedding:
    global _model

    if _model is None:
        os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT)
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        _model = TextEmbedding(model_name=MODEL_NAME)

    return _model


def _embed_documents(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.empty((0, EMBEDDING_DIM), dtype=np.float32)

    vectors = np.asarray(list(_get_model().embed(texts)), dtype=np.float32)
    expected_shape = (len(texts), EMBEDDING_DIM)
    if vectors.shape != expected_shape:
        raise ValueError(
            f"向量维度不匹配: expected={expected_shape}, actual={vectors.shape}"
        )
    return vectors


def _cosine_scores(query_vector: np.ndarray, document_vectors: np.ndarray) -> np.ndarray:
    query_norm = float(np.linalg.norm(query_vector))
    document_norms = np.linalg.norm(document_vectors, axis=1)
    denominator = document_norms * query_norm
    scores = np.zeros(len(document_vectors), dtype=np.float32)

    if query_norm == 0 or not np.any(denominator):
        return scores

    dots = document_vectors @ query_vector
    np.divide(dots, denominator, out=scores, where=denominator != 0)
    return scores


def _load_cache(questions: list[dict], questions_hash: str) -> np.ndarray | None:
    if not CACHE_PATH.is_file():
        return None

    try:
        with np.load(CACHE_PATH, allow_pickle=False) as cache:
            model_name = str(cache["model_name"].item())
            dimension = int(cache["dimension"].item())
            cached_hash = str(cache["questions_hash"].item())
            cached_ids = json.loads(str(cache["ids_json"].item()))
            vectors = np.asarray(cache["vectors"], dtype=np.float32)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None

    expected_ids = _question_ids(questions)
    expected_shape = (len(questions), EMBEDDING_DIM)
    if (
        model_name != MODEL_NAME
        or dimension != EMBEDDING_DIM
        or cached_hash != questions_hash
        or cached_ids != expected_ids
        or vectors.shape != expected_shape
        or not np.isfinite(vectors).all()
    ):
        return None

    return vectors


def _save_cache(questions: list[dict], questions_hash: str, vectors: np.ndarray) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = CACHE_PATH.with_suffix(".tmp.npz")
    np.savez_compressed(
        temporary_path,
        model_name=np.asarray(MODEL_NAME),
        dimension=np.asarray(EMBEDDING_DIM, dtype=np.int32),
        questions_hash=np.asarray(questions_hash),
        ids_json=np.asarray(json.dumps(_question_ids(questions), ensure_ascii=False)),
        vectors=vectors,
    )
    temporary_path.replace(CACHE_PATH)


def build_index() -> int:
    global _questions, _vectors, _index_ready

    questions = _load_questions()
    questions_hash = _questions_hash(questions)
    vectors = _load_cache(questions, questions_hash)

    if vectors is None:
        texts = [_question_text(question) for question in questions]
        vectors = _embed_documents(texts)
        _save_cache(questions, questions_hash, vectors)

    _questions = questions
    _vectors = vectors
    _index_ready = True
    return len(questions)


def search(query: str, top_k: int = 5) -> list[dict]:
    if top_k <= 0:
        return []

    query = query if isinstance(query, str) else str(query)
    if not query.strip():
        return []

    if not _index_ready:
        build_index()

    if _vectors is None:
        raise RuntimeError("向量索引未构建")

    query_vector = np.asarray(
        next(iter(_get_model().query_embed([query.strip()]))),
        dtype=np.float32,
    )
    if query_vector.shape != (EMBEDDING_DIM,):
        raise ValueError(
            f"查询向量维度不匹配: expected={(EMBEDDING_DIM,)}, actual={query_vector.shape}"
        )

    scores = _cosine_scores(query_vector, _vectors)
    ranked = np.argsort(-scores, kind="stable")[:top_k]

    results = []
    for index in ranked:
        question = _questions[int(index)]
        results.append(
            {
                "id": question.get("id"),
                "type": question.get("type"),
                "stem": question.get("stem"),
                "options": question.get("options") or [],
                "answer": question.get("answer"),
                "explanation": question.get("explanation"),
                "score": round(float(scores[int(index)]), 4),
            }
        )
    return results
