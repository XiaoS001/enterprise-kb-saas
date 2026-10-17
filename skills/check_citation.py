from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from skills.registry import Skill

CHECK_CITATION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "retrieved_results": {
            "type": "array",
            "items": {"type": "object"},
        },
    },
    "required": ["answer", "retrieved_results"],
}

# 🔴 引用格式对齐（RMB01 相对 XM03 的唯一改动）
#
# XM03 的正则只认 [12] / 【12】 / [Q12]，因为它的检索结果是「题目 id」。
# RMB01 检索的是**文档分段**，引用写成 [doc:<document_id>] —— 语义更清楚，
# 能指到具体哪一篇的哪一段。所以这里扩展了正则。
#
# 不改的后果（实测过）：'依据 [doc:12]' 提取到 []，于是
# citations=[] → invalid=[] → ok=True。**引用校验会静默失效**，
# 模型爱引用谁引用谁、甚至完全不引用都放行 —— 而这正是本项目最值钱的卖点。
_CITATION_PATTERN = re.compile(
    # [12] 【12】 [Q12] 【Q12】
    r"(?:\[|\u3010)\s*(?:Q|q)?[-_]?(\d+)\s*(?:\]|\u3011)"
    # [doc:12] 【doc:12】 [doc-12] [doc_12]  ← RMB03/RMB01 新增
    r"|(?:\[|\u3010)\s*doc[-_:](\d+)\s*(?:\]|\u3011)"
    # 裸写 Q4 / q_4（前面不能跟字母数字，避免命中 URL 片段）
    r"|(?<![A-Za-z0-9_])(?:Q|q)[-_]?(\d+)(?![A-Za-z0-9_])"
)

# 答案明确声明「查不到」时的措辞。
# 🔴 例外规则必须**具体且可枚举**，不能让模型随口一句「暂无相关内容」就绕过引用要求。
_NO_ANSWER_MARKERS = (
    '知识库中没有相关内容',
    '知识库中没有相关',
    '没有找到相关内容',
    '未找到相关内容',
    '知识库中未找到',
    '没有相关内容',
)


def _extract_result_id(result: Any) -> str | None:
    if isinstance(result, Mapping):
        for key in ("id", "question_id", "qid", "questionId"):
            value = result.get(key)
            if value is not None:
                return str(value)

        metadata = result.get("metadata")
        if isinstance(metadata, Mapping):
            for key in ("id", "question_id", "qid", "questionId"):
                value = metadata.get(key)
                if value is not None:
                    return str(value)

    for key in ("id", "question_id", "qid", "questionId"):
        value = getattr(result, key, None)
        if value is not None:
            return str(value)

    return None


def extract_citation_ids(answer: str) -> list[str]:
    citations: list[str] = []
    seen: set[str] = set()

    for match in _CITATION_PATTERN.finditer(answer):
        citation = next(group for group in match.groups() if group is not None)
        if citation not in seen:
            seen.add(citation)
            citations.append(citation)

    return citations


def declares_no_answer(answer: str) -> bool:
    """答案是否明确声明「知识库里查不到」。

    🔴 这是「零引用」的唯一合法豁免，且必须**具体到措辞**：
    如果放宽成「答案里没有引用就豁免」，那等于给了一条绕过校验的后门 ——
    模型只要不说引用就永远合格，引用校验形同虚设。
    所以只认下面这些明确措辞，其余情况一律要求引用。
    """

    return any(marker in answer for marker in _NO_ANSWER_MARKERS)


def check_citation(
    answer: str,
    retrieved_results: Iterable[Any],
) -> dict[str, Any]:
    citations = extract_citation_ids(answer)
    retrieved_ids = {
        result_id
        for result in retrieved_results
        if (result_id := _extract_result_id(result)) is not None
    }
    invalid_citations = [
        citation for citation in citations if citation not in retrieved_ids
    ]

    result = {
        "ok": not invalid_citations,
        "citations": citations,
        "retrieved_ids": sorted(retrieved_ids),
        "invalid_citations": invalid_citations,
    }

    # 🔴 「检索到证据但答案一个都没引用」也算不合格 ——
    # 原实现里 citations=[] → ok=True，等于「不引用就免检」。
    #
    # 但要注意：这两种不合格的**可自愈性不同**：
    #   · invalid_citations 非空 → 答案引用了不存在的证据，换查询重来有意义 → 应重试
    #   · 零引用              → 重试后大概率还是零引用（agent 会反复走
    #                          answerer→critic→answerer），实测会撞
    #                          GRAPH_RECURSION_LIMIT 而死循环
    # 所以用 retryable 字段区分，由调用方决定要不要重试。
    if retrieved_ids and not citations and not declares_no_answer(answer):
        result["ok"] = False
        result["reason"] = "有证据但未引用"
        result["retryable"] = False

    return result


def create_check_citation_skill() -> Skill:
    return Skill(
        name="check_citation",
        description="Check that answer citations point to ids present in retrieved results.",
        run=check_citation,
        input_schema=CHECK_CITATION_INPUT_SCHEMA,
    )


skill = create_check_citation_skill()
