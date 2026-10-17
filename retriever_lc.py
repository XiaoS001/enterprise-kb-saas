import json
from pathlib import Path

import jieba
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document


QUESTIONS_PATH = Path(__file__).resolve().parent / "data" / "questions.json"

_documents: list[Document] = []
_retriever: BM25Retriever | None = None
_questions_by_id: dict = {}
_index_ready = False


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


def build_index() -> int:
    global _documents, _retriever, _questions_by_id, _index_ready

    questions = _load_questions()
    documents = [
        Document(
            page_content=_question_text(question),
            metadata={
                "id": question.get("id"),
                "type": question.get("type"),
                "stem": question.get("stem"),
                "answer": question.get("answer"),
            },
        )
        for question in questions
    ]

    _documents = documents
    _questions_by_id = {question.get("id"): question for question in questions}
    _retriever = BM25Retriever.from_documents(
        documents,
        preprocess_func=jieba.lcut,
    )
    _index_ready = True
    return len(documents)


def search(query: str, top_k: int = 5) -> list[dict]:
    if top_k <= 0:
        return []

    if not _index_ready:
        build_index()

    query_tokens = jieba.lcut(query if isinstance(query, str) else str(query))
    if not query_tokens or _retriever is None:
        return []

    scores = _retriever.vectorizer.get_scores(query_tokens)
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
    ranked = [(index, score) for index, score in ranked if score > 0]

    results = []
    for index, score in ranked[:top_k]:
        document = _documents[index]
        question = _questions_by_id.get(document.metadata.get("id"), document.metadata)
        results.append(
            {
                "id": question.get("id"),
                "type": question.get("type"),
                "stem": question.get("stem"),
                "answer": question.get("answer"),
                "explanation": question.get("explanation"),
                "score": round(float(score), 1),
            }
        )
    return results
