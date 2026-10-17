"""🔴 知识库检索 —— 从 DB 取文档，按 kb_id + 租户隔离。

## 与 XM03 的区别（这是「从零做」的那部分）

XM03 的 retriever.py：
    _load_questions() 读**写死的** data/questions.json（685 道题）
    —— 全局一份，没有上传、没有分段、没有多库隔离

RMB01 改为：
    从 Document 表按 **kb_id + tenant_id** 取已分段的文档 → 建 BM25 索引

## 为什么用 BM25 而不是向量

任务单第四节：嵌入模型 BAAI/bge-small-zh-v1.5 是**运行时从 HuggingFace 下载**
的（约 90MB，仓库不预置）。默认开会让离线/容器首次运行失败。

所以默认走 BM25 —— 零网络依赖。向量路径保留但由 RMB01_VECTOR_ENABLED 控制，
且 CI 禁止运行（会用 @pytest.mark.vector 标记）。
"""
import logging

import jieba
from rank_bm25 import BM25Okapi

import config
from models import Document
from tenancy import scoped


logger = logging.getLogger('rmb01.search')


def tokenize(text):
    """中文分词。BM25 的输入。"""

    return [t for t in jieba.cut(text or '') if t.strip()]


def load_documents(kb_id, tenant_id):
    """🔴 取某知识库的文档分段 —— **必须经scoped 保证租户隔离**。

    注意这是阶段 2 隔离在检索层的延续：即使 kb_id 传对了，
    租户不对也取不到任何东西。
    """

    rows = (
        scoped(Document, tenant_id)
        .filter_by(kb_id=kb_id)
        .order_by(Document.id)
        .all()
    )
    return [
        {
            'document_id': doc.id,
            'title': doc.title,
            'chunk_index': doc.chunk_index,
            'content': doc.content,
        }
        for doc in rows
    ]


def search_documents(kb_id, query, top_k=3, tenant_id=None):
    """在某知识库里做 BM25 检索。

    tenant_id 缺省从会话取（current_tenant_id）。
    向量路径由 config.VECTOR_ENABLED 控制，默认关闭。
    """

    if tenant_id is None:
        from tenancy import get_current_tenant_id
        tenant_id = get_current_tenant_id()

    if tenant_id is None:
        # 🔴 没有租户上下文 → 不做跨租户检索（宁可无结果也不泄漏）
        return []

    documents = load_documents(kb_id, tenant_id)
    if not documents:
        return []

    if config.VECTOR_ENABLED:
        # 向量路径保留能力，但默认不启用（见模块 docstring）
        logger.warning('vector_path_not_enabled_yet', extra={
            'event': 'vector_path_not_enabled_yet',
        })

    corpus = [tokenize(doc['content']) for doc in documents]
    if not any(corpus):
        return []

    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(tokenize(query))

    ranked = sorted(
        zip(documents, scores), key=lambda pair: pair[1], reverse=True,
    )[:max(1, top_k)]

    # 🔴 语料只有 1 篇时，BM25 的 idf 会是**负数**（词出现在全部文档里
    #   → idf = log((N-df+0.5)/(df+0.5+1))，N=df=1 时为负）。
    #   所以 `score > 0` 会把唯一那篇也滤掉 —— 「单文档库永远检索不到」。
    #   正确做法：语料大于 1 篇时才用 >0 过滤；只有 1 篇时看它是否有命中。
    if len(documents) == 1:
        candidates = ranked          # 单文档：BM25 分数无区分度，不过滤
    else:
        candidates = [pair for pair in ranked if pair[1] > 0]

    return [
        {**doc, 'score': round(float(score), 4)}
        for doc, score in candidates
    ]


def format_citations(hits):
    """把检索结果转成给模型看的证据文本（含文档引用）。

    引用格式：[doc:<document_id>] —— 让answerer 能引用到**具体哪一段**，
    而不是笼统地说「知识库里有相关内容」。
    """

    if not hits:
        return ''
    lines = []
    for hit in hits:
        lines.append(
            f"[doc:{hit['document_id']}] {hit['title']}#{hit['chunk_index']}\n"
            f"    {hit['content'][:300]}"
        )
    return '\n'.join(lines)