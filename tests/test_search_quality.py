"""检索质量测试 —— 停用词与相关性过滤。

🔴 这组测试防的是「引用不相关」这个真实问题：

实测踩到：「慢查询怎么排查」把《产品FAQ》的「额度**怎么**算」
召回成第 2 名（2.78 分），于是答案引用了完全不相关的段落。
根因是「怎么」这种疑问词在小语料里仍是有效 term。
"""
import pytest

pytest_plugins = ['tenant_fixtures']

import config
from kb_search import STOPWORDS, search_documents, tokenize
from models import Document, Kb, Tenant, db


DOCS = [
    ('运维手册', '连接池大小建议按 CPU 核数乘以 2 再加磁盘数估算。'),
    ('运维手册', '慢查询排查按以下顺序：先看 EXPLAIN 输出确认索引是否命中。'),
    ('运维手册', '缓存穿透的标准解法有两种：布隆过滤器或空结果短期缓存。'),
    ('产品FAQ', '如何新建知识库？进入知识库页面点新建。'),
    ('产品FAQ', '额度怎么算？每次问答扣 1 次，降级不扣。'),
]


@pytest.fixture()
def kb_with_docs(app_with_db):
    tenant = Tenant(name='检索测试', slug='search')
    db.session.add(tenant)
    db.session.commit()
    kb = Kb(tenant_id=tenant.id, name='混合文档库')
    db.session.add(kb)
    db.session.commit()

    for title, content in DOCS:
        db.session.add(Document(
            tenant_id=tenant.id, kb_id=kb.id, title=title,
            content=content, chunk_index=0,
        ))
    db.session.commit()
    return {'tenant_id': tenant.id, 'kb_id': kb.id}


# ============ 停用词 ============
def test_stopwords_removed_from_query():
    """🔴 疑问词不进 BM25 —— 这是噪声的根源。"""

    tokens = tokenize('慢查询怎么排查')

    assert '怎么' not in tokens
    assert '慢' in tokens and '查询' in tokens and '排查' in tokens


def test_stopwords_do_not_include_content_words():
    """🔴 停用词表不能误伤实词。

    「配置」「查询」「额度」这些是业务词，去掉会让检索变瞎。
    """

    for word in ['配置', '查询', '额度', '索引', '缓存', '连接池']:
        assert word not in STOPWORDS, f'「{word}」是实词，不该进停用词表'


def test_question_words_all_in_stopwords():
    """常见疑问词都该被过滤。"""

    for word in ['怎么', '如何', '什么', '哪些', '是否', '可以']:
        assert word in STOPWORDS


# ============ 相关性过滤 ============
def test_shipped_query_only_returns_relevant_doc(app_with_db, kb_with_docs):
    """🔴 回归：「慢查询怎么排查」不该召回「额度怎么算」。"""

    hits = search_documents(
        kb_with_docs['kb_id'], '慢查询怎么排查',
        top_k=3, tenant_id=kb_with_docs['tenant_id'],
    )

    assert hits, '应召回到内容'
    assert '慢查询' in hits[0]['content']
    for hit in hits:
        assert '额度' not in hit['content'], (
            f'🔴 召回了不相关段落：{hit["content"][:40]}'
        )


def test_quota_query_returns_quota_doc(app_with_db, kb_with_docs):
    hits = search_documents(
        kb_with_docs['kb_id'], '额度怎么算',
        top_k=3, tenant_id=kb_with_docs['tenant_id'],
    )

    assert hits
    assert '额度' in hits[0]['content']


def test_unrelated_query_returns_empty(app_with_db, kb_with_docs):
    """🔴 库里没有的内容不该硬召回。"""

    hits = search_documents(
        kb_with_docs['kb_id'], '公司年会在哪里办',
        top_k=3, tenant_id=kb_with_docs['tenant_id'],
    )

    assert hits == []


def test_single_document_kb_is_searchable(app_with_db):
    """🔴 回归：单文档库曾经永远检索不到（BM25 idf 为负 → score>0 全滤掉）。"""

    tenant = Tenant(name='单词库', slug='single')
    db.session.add(tenant)
    db.session.commit()
    kb = Kb(tenant_id=tenant.id, name='只有一个文档')
    db.session.add(kb)
    db.session.commit()
    db.session.add(Document(
        tenant_id=tenant.id, kb_id=kb.id, title='唯一文档',
        content='连接池大小应按 CPU 核数乘以 2 估算。', chunk_index=0,
    ))
    db.session.commit()

    hits = search_documents(kb.id, '连接池大小', top_k=3, tenant_id=tenant.id)

    assert len(hits) == 1, '🔴 单文档库必须能检索到'


def test_top_k_respected(app_with_db, kb_with_docs):
    hits = search_documents(
        kb_with_docs['kb_id'], '知识库页面新建',
        top_k=1, tenant_id=kb_with_docs['tenant_id'],
    )
    assert len(hits) <= 1


def test_relative_threshold_configurable(monkeypatch, app_with_db, kb_with_docs):
    """阈值可配 —— 调高会过滤更严。"""

    monkeypatch.setattr(config, 'BM25_RELATIVE_THRESHOLD', 0.99)
    strict = search_documents(
        kb_with_docs['kb_id'], '慢查询怎么排查',
        top_k=3, tenant_id=kb_with_docs['tenant_id'],
    )

    monkeypatch.setattr(config, 'BM25_RELATIVE_THRESHOLD', 0.01)
    loose = search_documents(
        kb_with_docs['kb_id'], '慢查询怎么排查',
        top_k=3, tenant_id=kb_with_docs['tenant_id'],
    )

    assert len(strict) <= len(loose)


def test_search_requires_tenant_id(app_with_db, kb_with_docs):
    """无租户上下文 → 返回空（宁可无结果也不跨租户泄漏）。"""

    assert search_documents(kb_with_docs['kb_id'], '慢查询', top_k=3) == []


def test_search_scoped_by_kb(app_with_db, kb_with_docs):
    """检索按 kb_id 隔离 —— 别的库的内容不能召回。"""

    tenant_id = kb_with_docs['tenant_id']
    other_kb = Kb(tenant_id=tenant_id, name='另一个库')
    db.session.add(other_kb)
    db.session.commit()
    db.session.add(Document(
        tenant_id=tenant_id, kb_id=other_kb.id, title='别的库',
        content='这是另一个库里独有的机密标记XYZ。', chunk_index=0,
    ))
    db.session.commit()

    hits = search_documents(
        kb_with_docs['kb_id'], '机密标记XYZ', top_k=5, tenant_id=tenant_id,
    )

    assert all('XYZ' not in h['content'] for h in hits), '🔴 跨库召回了'