import json
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi


QUESTIONS_PATH = Path(__file__).resolve().parent / "data" / "questions.json"


def _normalize_options(options) -> list[dict]:
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            return []
    if not isinstance(options, list):
        return []
    return [option for option in options if isinstance(option, dict)]

STOP_WORDS = {
    "的",
    "了",
    "是",
    "在",
    "和",
    "有",
    "为",
    "与",
    "及",
    "一个",
    "什么",
    "哪些",
    "我",
    "你",
    "他",
    "她",
    "它",
    "这",
    "那",
    "就",
    "都",
    "而",
    "或",
    "被",
    "把",
    "对",
    "从",
    "到",
    "中",
    "上",
    "下",
    "里",
    "后",
    "前",
    "吗",
    "呢",
    "啊",
    "可以",
    "进行",
    "以及",
    "这个",
    "那个",
    "一种",
    "其中",
    "通过",
    ".",
    "。",
    ",",
    "，",
    ";",
    "；",
    ":",
    "：",
    "?",
    "？",
    "!",
    "！",
    "、",
}

_questions: list[dict] = []
_bm25: BM25Okapi | None = None
_index_ready = False


def _tokenize(text: str) -> list[str]:
    tokens = []
    for token in jieba.lcut(text):
        normalized = token.strip().lower()
        if normalized and normalized not in STOP_WORDS:
            tokens.append(normalized)
    return tokens


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


def build_index() -> int:
    global _questions, _bm25, _index_ready

    questions = _load_questions()
    tokenized_questions = [_tokenize(_question_text(question)) for question in questions]

    _questions = questions
    _bm25 = BM25Okapi(tokenized_questions) if tokenized_questions else None
    _index_ready = True
    return len(questions)


def search(query: str, top_k: int = 5) -> list[dict]:
    if top_k <= 0:
        return []

    if not _index_ready:
        build_index()

    query_tokens = _tokenize(query if isinstance(query, str) else str(query))
    if not query_tokens or _bm25 is None:
        return []

    scores = _bm25.get_scores(query_tokens)
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
    ranked = [(index, score) for index, score in ranked if score > 0]

    results = []
    for index, score in ranked[:top_k]:
        question = _questions[index]
        results.append(
            {
                "id": question.get("id"),
                "type": question.get("type"),
                "stem": question.get("stem"),
                "options": question.get("options") or [],
                "answer": question.get("answer"),
                "explanation": question.get("explanation"),
                "score": round(float(score), 1),
            }
        )
    return results
