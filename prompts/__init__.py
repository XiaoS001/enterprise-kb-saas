from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

PROMPT_VERSIONS = {
    "planner": "v1",
    "critic": "v1",
    "answerer": "qa_lc.v1",
}

EVALUATE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你负责判断给定材料是否足以完整回答用户问题。\n"
            "只输出合法 JSON，不要输出 Markdown 代码块或额外说明。\n"
            'JSON 格式必须为：{"enough": true/false, "reason": "..."}',
        ),
        ("human", "用户问题：{question}\n\n当前材料：\n{materials}"),
    ]
)

REWRITE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你负责改写题库检索查询。\n"
            "只输出一条新的中文检索查询，不要解释，不要编号，"
            "不要重复用户已经尝试过的查询。",
        ),
        (
            "human",
            "原始问题：{question}\n"
            "已经检索过的查询：{queries}\n"
            "当前是第 {next_attempt} 轮检索，请给出一个补充查询。",
        ),
    ]
)

__all__ = [
    "EVALUATE_PROMPT",
    "REWRITE_PROMPT",
    "PROMPT_VERSIONS",
]
