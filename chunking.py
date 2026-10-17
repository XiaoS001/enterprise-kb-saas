"""文档分段与索引。

🔴 **分段为什么不用默认的 `\\n`**：XM03 踩过这个坑 ——
默认按换行切会把句子拦腰截断，一段话被切得七零八落，
检索出来读不懂���BM25 也就召不回真正相关的内容。

所以这里用「**两级策略**」：
1. 先按**自定义分隔符**（默认空行 `\\n\\n`，即 Markdown 段落边界）切
2. 段落过长时再按句子边界（。！？；）二次切分，最后才硬切

每段都带chunk_index���便于引用时指回原文位置。
"""
import re

import config


# 句子边界：中英文标点。
# 🔴 中文里句号后面通常**没有空格**（「第一句。第二句！」），所以不能要求
#    标点后必须有空白 —— 那样会把整段当成一句，段落过长时就只能硬切。
#    正确做法：按「标点本身」切分，不看标点后有没有空格。
#    英文里句号后通常有空格，但用 lookbehind 不受影响。
_SENTENCE_SPLIT = re.compile(r'(?<=[。！？；!?;])\s*|\s+(?=[.!?;])')


def split_sentences(text):
    """把一段文本切成句子（保留标点）。"""

    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def chunk_text(text, separator=None, max_chars=None):
    """把文本分段。

    返回 list[str]，顺序即原文顺序。

    参数默认值来自 config，测试可注入不同参数。
    """

    if separator is None:
        separator = config.CHUNK_SEPARATOR
    if max_chars is None:
        max_chars = config.CHUNK_MAX_CHARS

    if not text or not text.strip():
        return []

    # 第一级：按自定义分隔符（通常是空行）切成段落
    paragraphs = [p.strip() for p in text.split(separator) if p.strip()]

    chunks = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            chunks.append(paragraph)
            continue

        # 第二级：段落太长 → 按句子边界切，贪心地装进 max_chars 的框
        buffer = ''
        for sentence in split_sentences(paragraph):
            if buffer and len(buffer) + len(sentence) + 1 > max_chars:
                chunks.append(buffer.strip())
                buffer = sentence
            else:
                buffer = f'{buffer} {sentence}'.strip() if buffer else sentence

            # 单句就超长（比如没有标点的一长串）→ 硬切，别死循环
            while len(buffer) > max_chars:
                chunks.append(buffer[:max_chars])
                buffer = buffer[max_chars:]

        if buffer.strip():
            chunks.append(buffer.strip())

    return chunks


def build_document_chunks(kb_id, title, text, uploader_id=None, separator=None,
                          max_chars=None):
    """分段并落库。返回 list[Document]（已 commit）。"""

    from models import Document, db

    chunks = chunk_text(text, separator=separator, max_chars=max_chars)
    created = []
    for index, content in enumerate(chunks):
        document = Document(
            tenant_id=None,      # 由调用方通过 tenant_fields 强制覆盖
            kb_id=kb_id,
            uploader_id=uploader_id,
            title=title,
            chunk_index=index,
            content=content,
        )
        db.session.add(document)
        created.append(document)

    db.session.commit()
    return created