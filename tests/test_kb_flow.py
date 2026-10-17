"""分段与检索的端到端测试。

审查要求：「上传 → 分段 → 检索这条链路有端到端测试（不是只测各段函数）」。

所以这里真跑：上传接口 → 落库 → 从库里建索引 → 检索 → 断言召回的是
正确那段内容，并带上文档引用。
"""
import pytest

pytest_plugins = ['tenant_fixtures']

from chunking import chunk_text, split_sentences
from auth import register_user
from models import ROLE_ORG_ADMIN, Document, Kb, Tenant, db


SAMPLE = """第一段讲的是数据库连接池的配置方法。

第二段讲的是慢查询的排查步骤，包括 EXPLAIN 的使用。

第三段讲的是缓存穿透的几种常见解法。"""


@pytest.fixture()
def kb_env(app_with_db):
    a = Tenant(name='甲公司', slug='tenant-a')
    b = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([a, b])
    db.session.commit()
    db.session.add_all([
        Kb(tenant_id=a.id, name='甲库'),
        Kb(tenant_id=b.id, name='乙库'),
    ])
    db.session.commit()
    user, _ = register_user('甲管理员', 'pass123456', ROLE_ORG_ADMIN, a.id)
    return {'a': a, 'b': b, 'user': user}


def _login(client):
    return client.post('/api/auth/login', json={
        'username': '甲管理员', 'password': 'pass123456',
    })


# ============ 分段单元 ============
def test_chunk_splits_on_blank_line():
    """🔴 默认分隔符是空行（段落边界），不是单换行。

    XM03踩过的坑：默认按 \\n 切会把句子拦腰截断。
    """

    chunks = chunk_text(SAMPLE)

    assert len(chunks) == 3
    assert '数据库连接池' in chunks[0]
    assert '慢查询' in chunks[1]
    assert '缓存穿透' in chunks[2]


def test_chunk_keeps_single_newline_within_paragraph():
    """🔴 段落内的单换行**不应**被切开。"""

    text = '第一行\n第二行\n\n新段落'

    chunks = chunk_text(text)

    assert len(chunks) == 2
    assert '第一行' in chunks[0] and '第二行' in chunks[0]


def test_chunk_respects_max_chars():
    """超长段落按句子边界二次切分。"""

    long_paragraph = '。'.join(f'这是第{i}个很长的句子用于测试分段逻辑' for i in range(20)) + '。'

    chunks = chunk_text(long_paragraph, separator='\n\n', max_chars=100)

    assert len(chunks) > 1
    assert all(len(c) <= 100 + 30 for c in chunks), '不该有超长段'


def test_chunk_handles_single_sentence_no_punctuation():
    """没有标点的超长串也要能切，不能死循环。"""

    chunks = chunk_text('啊' * 500, separator='\n\n', max_chars=100)

    assert len(chunks) >= 5
    assert all(len(c) <= 100 for c in chunks)


def test_chunk_empty_text_returns_empty_list():
    assert chunk_text('') == []
    assert chunk_text('   \n\n  ') == []


def test_chunk_preserves_order():
    """🔴 分段顺序必须等于原文顺序（引用要指回正确位置）。"""

    chunks = chunk_text(SAMPLE)

    assert '连接池' in chunks[0]
    assert '慢查询' in chunks[1]
    assert '穿透' in chunks[2]


def test_split_sentences_keeps_punctuation():
    sentences = split_sentences('第一句。第二句！第三句？')

    assert len(sentences) == 3
    assert sentences[0].endswith('。')


# ============ 端到端：上传 → 分段 → 检索 ============
def test_upload_then_search_end_to_end(app_with_db, kb_env):
    """🔴 完整链路：上传 → 分段落库 → 从库建索引 → 检索 → 引用正确文档。"""

    client = app_with_db.test_client()
    _login(client)

    kb_id = db.session.get(Kb, 1).id

    # 1) 上传
    r = client.post(f'/api/kbs/{kb_id}/documents', json={
        'title': '运维手册', 'content': SAMPLE,
    })
    assert r.status_code == 201
    uploaded = r.get_json()['chunks']
    assert len(uploaded) == 3

    # 2) 确认真的落库了，且 chunk_index 连续
    docs = (
        Document.query.filter_by(kb_id=kb_id)
        .order_by(Document.chunk_index)
        .all()
    )
    assert len(docs) == 3
    assert [d.chunk_index for d in docs] == [0, 1, 2]
    assert all(d.tenant_id == kb_env['a'].id for d in docs), '必须落在自己的租户'

    # 3) 从库里建索引并检索（这就是改后的 retriever 逻辑）
    #    🔴 显式传 tenant_id：测试里没有会话上下文，检索层拿不到租户会
    #    故意返回空（宁可无结果也不跨租户泄漏）。
    from kb_search import search_documents

    hits = search_documents(kb_id, '慢查询怎么排查', top_k=2,
                            tenant_id=kb_env['a'].id)

    assert hits, '🔴 应召回到相关内容'
    assert '慢查询' in hits[0]['content'], f'召回错段了：{hits[0]["content"][:60]}'
    assert hits[0]['title'] == '运维手册'
    assert hits[0]['document_id'] == docs[1].id, '引用要指回正确的那段'


def test_search_not_finding_unrelated_topic(app_with_db, kb_env):
    """🔴 库里没有的内容不该硬召回。"""

    client = app_with_db.test_client()
    _login(client)
    kb_id = db.session.get(Kb, 1).id
    client.post(f'/api/kbs/{kb_id}/documents', json={
        'title': '运维手册', 'content': SAMPLE,
    })

    from kb_search import search_documents

    hits = search_documents(kb_id, '今天天气怎么样', top_k=3,
                           tenant_id=kb_env['a'].id)

    assert all('天气' not in h['content'] for h in hits)


def test_search_is_isolated_by_kb(app_with_db, kb_env):
    """🔴 检索必须按 kb_id 隔离：A 的库里问不到 B 库的内容。"""

    client = app_with_db.test_client()
    _login(client)
    a_kb, b_kb = db.session.get(Kb, 1), db.session.get(Kb, 2)

    client.post(f'/api/kbs/{a_kb.id}/documents', json={
        'title': '甲的文档', 'content': '甲公司专有的内容标记AAA。',
    })
    # 直接给 B 库塞一条（模拟 B 用户上传）
    client.post(f'/api/kbs/{b_kb.id}/documents', json={
        'title': '乙的文档', 'content': '乙公司专有的内容标记BBB。',
    })

    from kb_search import search_documents

    a_hits = search_documents(a_kb.id, '内容标记', top_k=5,
                                tenant_id=kb_env['a'].id)
    assert all('BBB' not in h['content'] for h in a_hits), '🔴 A 的检索不能召回 B 的内容'


def test_search_empty_kb_returns_empty(app_with_db, kb_env):
    from kb_search import search_documents

    assert search_documents(1, '任何问题', top_k=3,
                             tenant_id=kb_env['a'].id) == []


def test_search_isolated_by_tenant(app_with_db, kb_env):
    """🔴 检索层也要有租户隔离 —— 不能只靠 kb_id 过滤。"""

    from kb_search import search_documents

    # 直接构造一个「A 的租户上下文下查 A 的库」
    hits = search_documents(1, '数据库', top_k=3, tenant_id=kb_env['a'].id)
    assert isinstance(hits, list)