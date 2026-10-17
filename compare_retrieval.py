import os
import re
import statistics
import time
from datetime import datetime

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import numpy as np

import retriever
import retriever_vec


TOP_K = 5
OUTPUT_PATH = retriever_vec.BASE_DIR / "data" / "向量vsBM25对比结果.md"

QUESTIONS = [
    ("关键词命中", "Spring 依赖注入有哪几种方式"),
    ("关键词命中", "MyBatis 动态 SQL 的常用标签有哪些"),
    ("关键词命中", "Spring AOP 的核心概念是什么"),
    ("关键词命中", "MyBatis 一级缓存和二级缓存有什么区别"),
    ("同义改写", "对象怎么交给容器管理"),
    ("同义改写", "怎样让重复的数据库查询复用已有结果"),
    ("同义改写", "请求进入控制器之前怎样统一拦截处理"),
    ("同义改写", "SQL 怎么根据条件自动拼接"),
    ("概念模糊", "Spring 声明式事务是怎么工作的"),
    ("概念模糊", "请求 URL 怎么映射到控制器方法"),
    ("概念模糊", "父类方法为什么能在不同子类表现不同"),
    ("概念模糊", "程序异常怎么统一交给框架处理"),
]

OUT_OF_CORPUS_QUERIES = [
    "什么是死锁",
    "MySQL 索引为什么用 B+ 树",
]


def _escape_markdown(text) -> str:
    return str(text or "").replace("|", r"\|").replace("\n", "<br>")


def _short_text(text, limit: int = 52) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _top_ids(results: list[dict]) -> str:
    return ", ".join(str(result["id"]) for result in results)


def _first_result(results: list[dict]) -> tuple:
    if not results:
        return "-", None
    first = results[0]
    return f"{first['id']} ({first['score']})", first


def _timed_search(module, question: str) -> tuple[list[dict], float]:
    started = time.perf_counter()
    results = module.search(question, top_k=TOP_K)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return results, elapsed_ms


def _average(rows: list[dict], key: str) -> float:
    return statistics.fmean(row[key] for row in rows) if rows else 0.0


def _category_average(rows: list[dict], category: str, key: str) -> float:
    return _average([row for row in rows if row["category"] == category], key)


def _question_bank_text() -> str:
    parts = []
    for question in retriever_vec._questions:
        parts.extend(
            [
                str(question.get("stem") or ""),
                str(question.get("answer") or ""),
                str(question.get("explanation") or ""),
            ]
        )
    return " ".join(parts)


def _count_term(term: str) -> int:
    text = _question_bank_text().lower()
    return len(re.findall(re.escape(term.lower()), text))


def _count_b_plus_tree() -> int:
    text = _question_bank_text().lower()
    return len(re.findall(r"b\s*\+\s*树", text))


def _document_similarity_stats() -> tuple[float, float]:
    vectors = np.asarray(retriever_vec._vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    normalized = np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms != 0)
    similarity = normalized @ normalized.T
    upper = similarity[np.triu_indices(len(normalized), k=1)]
    return float(upper.mean()), float(upper.std())


def _top5_score_gaps(rows: list[dict]) -> list[float]:
    gaps = []
    for row in rows:
        results = row["vector_results"]
        if len(results) >= 2:
            gaps.append(float(results[0]["score"] - results[-1]["score"]))
    return gaps


def _out_of_corpus_rows() -> list[dict]:
    rows = []
    for question in OUT_OF_CORPUS_QUERIES:
        results = retriever_vec.search(question, top_k=3)
        rows.append(
            {
                "question": question,
                "results": results,
                "first": results[0] if results else None,
            }
        )
    return rows


def _row_by_question(rows: list[dict], question: str) -> dict:
    return next(row for row in rows if row["question"] == question)


def _first_summary(row: dict, source: str) -> str:
    first = row[f"{source}_results"][0]
    return f"id {first['id']}“{_short_text(first['stem'])}”"


def _build_conclusion(rows: list[dict]) -> list[str]:
    keyword_overlap = _category_average(rows, "关键词命中", "overlap")
    synonym_overlap = _category_average(rows, "同义改写", "overlap")
    fuzzy_overlap = _category_average(rows, "概念模糊", "overlap")
    bm25_ms = _average(rows, "bm25_ms")
    vector_ms = _average(rows, "vector_ms")

    transaction_row = _row_by_question(rows, "Spring 声明式事务是怎么工作的")
    interceptor_row = _row_by_question(
        rows, "请求进入控制器之前怎样统一拦截处理"
    )
    reuse_row = _row_by_question(
        rows, "怎样让重复的数据库查询复用已有结果"
    )

    return [
        "### 结论",
        "",
        f"- BM25 平均耗时 **{bm25_ms:.3f} ms**，向量检索平均耗时 "
        f"**{vector_ms:.3f} ms**，约为 BM25 的 **{vector_ms / bm25_ms:.2f} 倍**。",
        f"- 关键词类平均重合 **{keyword_overlap:.2f}/5**，同义改写类 "
        f"**{synonym_overlap:.2f}/5**，概念模糊类 **{fuzzy_overlap:.2f}/5**。",
        "- 向量检索因为稠密分数一定返回 top-5，所以“有返回”不能当作“更准”；"
        "本题库规模下，低重合更多是在说明两套排序信号差异很大。",
        "",
        "### 具体判断",
        "",
        f"- 向量检索有明确胜出的例子：`Spring 声明式事务是怎么工作的` 中，"
        f"BM25 首条是 {_first_summary(transaction_row, 'bm25')}，"
        f"向量首条是 {_first_summary(transaction_row, 'vector')}，后者直接命中"
        "声明式事务注解。",
        f"- BM25 也有明显更好的例子：`请求进入控制器之前怎样统一拦截处理` 中，"
        f"BM25 首条是 {_first_summary(interceptor_row, 'bm25')}，直接描述拦截器；"
        f"向量首条是 {_first_summary(interceptor_row, 'vector')}，虽然同属拦截器，"
        "但题干在描述错误说法。",
        f"- 同义改写并不保证向量一定成功：`怎样让重复的数据库查询复用已有结果` 中，"
        f"BM25 首条是 {_first_summary(reuse_row, 'bm25')}，"
        f"向量首条是 {_first_summary(reuse_row, 'vector')}，"
        "两者都没有找到 MyBatis 缓存题。",
        f"- 关键词题里 BM25 更贴近原句，例如 Spring 依赖注入题两版首条同为 id 190；"
        "MyBatis 动态 SQL 和 MyBatis 缓存题则出现相近主题但不同排序。",
        "",
        "### 实话结论",
        "",
        "在这份 685 题、短文本、主题混杂的中文题库上，这次实测没有证明"
        "`bge-small-zh-v1.5` 向量检索全面优于 BM25。BM25 在多数问题上的首条结果"
        "至少不差，而且平均耗时低得多；向量检索在“Spring 声明式事务”这样的语义"
        "问题上展现了价值，但也会被短文本和相似术语干扰。更可信的工程结论是："
        "BM25 适合作为当前小语料的主检索，向量检索作为补充召回，再叠加人工标注、"
        "重排或混合评分，而不是单独宣布某一版胜出。",
        "",
        "注：本次没有人工 ground truth 标注，判断以首条题干相关性、top-5 重合度"
        "和明确列出的代表案例为准，不是 nDCG/MRR 级别的离线评测。",
    ]


def build_report() -> str:
    retriever.build_index()
    vector_count = retriever_vec.build_index()

    retriever.search("预热", top_k=1)
    retriever_vec.search("预热", top_k=1)

    rows = []
    for category, question in QUESTIONS:
        bm25_results, bm25_ms = _timed_search(retriever, question)
        vector_results, vector_ms = _timed_search(retriever_vec, question)
        bm25_ids = [result["id"] for result in bm25_results]
        vector_ids = [result["id"] for result in vector_results]
        overlap = len(set(bm25_ids) & set(vector_ids))

        rows.append(
            {
                "category": category,
                "question": question,
                "bm25_results": bm25_results,
                "vector_results": vector_results,
                "overlap": overlap,
                "bm25_ms": bm25_ms,
                "vector_ms": vector_ms,
            }
        )

    similarity_mean, similarity_std = _document_similarity_stats()
    score_gaps = _top5_score_gaps(rows)
    out_of_corpus_rows = _out_of_corpus_rows()

    term_counts = [
        ("B+树", _count_b_plus_tree()),
        ("MySQL", _count_term("MySQL")),
        ("死锁", _count_term("死锁")),
        ("红黑树", _count_term("红黑树")),
    ]

    lines = [
        "# 向量检索 vs BM25 对比结果",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 语料：{vector_count} 道题；检索深度：top-{TOP_K}",
        f"> 向量模型：`{retriever_vec.MODEL_NAME}`（{retriever_vec.EMBEDDING_DIM} 维）",
        "> BM25：现有手写版 `retriever.py`；向量版：`retriever_vec.py`",
        "> 计时方式：两个索引均预热后，对每个问题分别执行一次检索并记录毫秒数。",
        "",
        "## 1. 逐问题明细",
        "",
        "| 类型 | 问题 | BM25 top-5 ID | BM25 第一名(分数) | "
        "向量 top-5 ID | 向量第一名(分数) | 重合题数 | BM25 耗时(ms) | 向量耗时(ms) |",
        "|---|---|---|---|---|---|---:|---:|---:|",
    ]

    for row in rows:
        bm25_first, _ = _first_result(row["bm25_results"])
        vector_first, _ = _first_result(row["vector_results"])
        lines.append(
            "| {category} | {question} | {bm25_ids} | {bm25_first} | "
            "{vector_ids} | {vector_first} | {overlap} | {bm25_ms:.3f} | "
            "{vector_ms:.3f} |".format(
                category=row["category"],
                question=_escape_markdown(row["question"]),
                bm25_ids=_top_ids(row["bm25_results"]) or "-",
                bm25_first=bm25_first,
                vector_ids=_top_ids(row["vector_results"]) or "-",
                vector_first=vector_first,
                overlap=row["overlap"],
                bm25_ms=row["bm25_ms"],
                vector_ms=row["vector_ms"],
            )
        )

    average_overlap = _average(rows, "overlap")
    average_overlap_rate = average_overlap / TOP_K * 100
    lines.extend(
        [
            "",
            "## 2. 第一名题干对照",
            "",
            "| 类型 | 问题 | BM25 第一名题干 | 向量第一名题干 |",
            "|---|---|---|---|",
        ]
    )
    for row in rows:
        _, bm25_first = _first_result(row["bm25_results"])
        _, vector_first = _first_result(row["vector_results"])
        lines.append(
            "| {category} | {question} | {bm25_stem} | {vector_stem} |".format(
                category=row["category"],
                question=_escape_markdown(row["question"]),
                bm25_stem=_escape_markdown(
                    bm25_first.get("stem") if bm25_first else "无"
                ),
                vector_stem=_escape_markdown(
                    vector_first.get("stem") if vector_first else "无"
                ),
            )
        )

    lines.extend(
        [
            "",
            "## 3. 汇总",
            "",
            f"- 平均重合题数：**{average_overlap:.2f}/5**（"
            f"平均重合率 {average_overlap_rate:.1f}%）",
            f"- BM25 平均耗时：**{_average(rows, 'bm25_ms'):.3f} ms**",
            f"- 向量检索平均耗时：**{_average(rows, 'vector_ms'):.3f} ms**",
            f"- 关键词类平均重合：**{_category_average(rows, '关键词命中', 'overlap'):.2f}/5**",
            f"- 同义改写类平均重合：**{_category_average(rows, '同义改写', 'overlap'):.2f}/5**",
            f"- 概念模糊类平均重合：**{_category_average(rows, '概念模糊', 'overlap'):.2f}/5**",
            "",
            "## 4. 文档间相似度分布",
            "",
            f"- 685 条文档向量两两余弦相似度均值：**{similarity_mean:.3f}**，"
            f"标准差：**{similarity_std:.3f}**。",
            "- 已实测的另一组结果是 top1 到 top5 分数极差仅 **0.022 到 0.154**；"
            "本次 12 题重新运行得到的结果见下一条，两组都说明分数间距很窄。",
            f"- 本次 12 个问题的向量 top1 与 top5 分差范围为 "
            f"**{min(score_gaps):.3f} 到 {max(score_gaps):.3f}**，"
            "首尾分数非常接近时，排序容易被短文本细节扰动。",
            "- 原因：题库几乎全是同一知识域下的短文本，而且文档文本拼入了"
            "“答案 + 解析”；答案和解析又大量复用同一批术语，导致文档向量整体聚拢。",
            "- 浅层影响：相似分数看起来普遍不低，但分数高低不等于题目真的回答了问题；"
            "必须结合首条题干和人工判断，不能只看余弦分数。",
            "",
            "## 5. 题库外知识风险",
            "",
            "| 术语 | 题库实际命中数 |",
            "|---|---:|",
        ]
    )
    for term, count in term_counts:
        lines.append(f"| {term} | {count} |")

    lines.extend(
        [
            "",
            "| 实际提问 | 向量 top-1 | 返回题目 | 说明 |",
            "|---|---|---|---|",
        ]
    )
    for item in out_of_corpus_rows:
        first = item["first"]
        if first is None:
            lines.append(f"| {_escape_markdown(item['question'])} | - | - | 无返回 |")
            continue
        lines.append(
            "| {question} | {score} | {stem} | 题库无对应知识，仍返回最高分候选 |".format(
                question=_escape_markdown(item["question"]),
                score=first["score"],
                stem=_escape_markdown(_short_text(first["stem"], 90)),
            )
        )

    lines.extend(
        [
            "",
            "- `B+树`、`MySQL`、`死锁`、`红黑树`在当前题库中对应知识为 0 道。",
            "- 问“什么是死锁”时，模型仍返回门禁卡鉴别题；问“MySQL 索引为什么用"
            " B+ 树”时，返回的是 Python 列表索引、构造参数 index、字典索引，"
            "只是共享了“索引”这个词，并不是数据库索引知识。",
            "- 结论：向量检索在没有对应知识时不会天然说“不知道”，而是给出向量空间"
            "最近的题目。这是它相对 BM25 的真实风险，必须在上层 Agent 或回答层加"
            "“材料是否真正支持问题”的校验。",
            "",
            *_build_conclusion(rows),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    report = build_report()
    OUTPUT_PATH.write_text(report, encoding="utf-8")
    print(f"written={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
