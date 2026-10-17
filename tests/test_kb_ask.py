"""问答端到端测试（阶段 3 第 5 项）。

覆盖复验指示第四节要求的 5 条：
  1. 问答接口接上 agent
  2. 引用指向 document（可解析回具体文档 + 段落）
  3. 上传 → 问 → 引用指向正确的 document_id；问库外问题 → 不乱引用
  4. 跨租户 ask → 404
  5. 拿掉 key 仍不 500，走降级
"""
import pytest

pytest_plugins = ['tenant_fixtures']

from auth import register_user
from models import (
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    Document,
    Kb,
    Tenant,
    db,
)


HANDBOOK = """数据库连接池配置指南。

连接池的大小应该根据 CPU 核数乘以 2 再加磁盘数来估算。

慢查询排查步骤：先看 EXPLAIN 输出，再看索引命中率。

缓存穿透的常见解法是布隆过滤器和空值缓存。"""


@pytest.fixture()
def kb_env(app_with_db):
    a = Tenant(name='甲公司', slug='tenant-a')
    b = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([a, b])
    db.session.commit()

    kb_a = Kb(tenant_id=a.id, name='甲的运维手册')
    kb_b = Kb(tenant_id=b.id, name='乙的私有手册')
    db.session.add_all([kb_a, kb_b])
    db.session.commit()

    db.session.add(Document(
        tenant_id=b.id, kb_id=kb_b.id, title='乙机密',
        content='乙公司的绝密内容标记ZZZ。', chunk_index=0,
    ))
    db.session.commit()

    register_user('甲管理员', 'pass123456', ROLE_ORG_ADMIN, a.id)
    register_user('乙管理员', 'pass123456', ROLE_ORG_ADMIN, b.id)
    register_user('甲成员', 'pass123456', ROLE_MEMBER, a.id)

    return {'a': a, 'b': b, 'kb_a': kb_a, 'kb_b': kb_b}


def _login(client, username='甲管理员'):
    return client.post('/api/auth/login', json={
        'username': username, 'password': 'pass123456',
    })


def _upload(client, kb_id, content=HANDBOOK, title='运维手册'):
    return client.post(f'/api/kbs/{kb_id}/documents', json={
        'title': title, 'content': content,
    })


# ============ 1 · 问答接口基本可用 ============
def test_ask_requires_login(app_with_db, kb_env):
    r = app_with_db.test_client().post(
        f"/api/kbs/{kb_env['kb_a'].id}/ask", json={'question': '连接池'},
    )
    assert r.status_code == 401


def test_ask_returns_references_to_documents(app_with_db, kb_env):
    """🔴 引用必须指向具体的 document + 段落。"""

    c = app_with_db.test_client()
    _login(c)
    kb_id = kb_env['kb_a'].id
    _upload(c, kb_id)

    r = c.post(f'/api/kbs/{kb_id}/ask', json={'question': '连接池大小怎么估算'})

    assert r.status_code == 200
    body = r.get_json()
    assert body['retrieved_count'] >= 1, '应召回到相关内容'
    assert body['references'], '🔴 必须返回引用'
    ref = body['references'][0]
    assert ref['document_id'] > 0
    assert ref['title'] == '运维手册'
    assert '连接池' in ref['preview']


def test_ask_requires_question(app_with_db, kb_env):
    c = app_with_db.test_client()
    _login(c)

    r = c.post(f"/api/kbs/{kb_env['kb_a'].id}/ask", json={'question': '  '})

    assert r.status_code == 400


# ============ 2 · 上传 → 问 → 引用正确 ============
def test_end_to_end_upload_ask_reference(app_with_db, kb_env):
    """🔴 完整链路：上传 → 问 → 引用指向正确的 document_id 与段落。"""

    c = app_with_db.test_client()
    _login(c)
    kb_id = kb_env['kb_a'].id

    upload = _upload(c, kb_id)
    assert upload.status_code == 201
    chunks = upload.get_json()['chunks']
    assert len(chunks) == 4, '空行分隔切成 4 段'

    # 问一个文档里有的问题
    r = c.post(f'/api/kbs/{kb_id}/ask', json={'question': '慢查询怎么排查'})
    assert r.status_code == 200
    body = r.get_json()

    assert body['retrieved_count'] >= 1
    top = body['references'][0]
    assert top['title'] == '运维手册'
    assert '慢查询' in top['preview'], f'召回了错误段落：{top["preview"][:50]}'
    # 引用的 document_id 必须真实存在于库
    with app_with_db.app_context():
        doc = db.session.get(Document, top['document_id'])
        assert doc is not None, '🔴 引用指向了不存在的文档'
        assert doc.kb_id == kb_id
        assert doc.chunk_index == top['chunk_index']


def test_ask_unrelated_question_does_not_fabricate(app_with_db, kb_env):
    """🔴 问库里没有的 → 如实说没有，不硬答、不乱引用。"""

    c = app_with_db.test_client()
    _login(c)
    kb_id = kb_env['kb_a'].id
    _upload(c, kb_id)

    r = c.post(f'/api/kbs/{kb_id}/ask', json={'question': '今天天气怎么样'})
    body = r.get_json()

    assert r.status_code == 200
    if body['retrieved_count'] == 0:
        assert body['answer'] == '知识库中没有相关内容。'
        assert body['references'] == []


def test_ask_empty_kb_says_no_content(app_with_db, kb_env):
    c = app_with_db.test_client()
    _login(c)

    r = c.post(f"/api/kbs/{kb_env['kb_a'].id}/ask", json={'question': '任何问题'})
    body = r.get_json()

    assert body['answer'] == '知识库中没有相关内容。'
    assert body['retrieved_count'] == 0


# ============ 3 · 跨租户 → 404 ============
def test_ask_other_tenant_kb_returns_404(app_with_db, kb_env):
    """🔴 A 问 B 的库 → 404（不是 403）。"""

    c = app_with_db.test_client()
    _login(c)

    r = c.post(f"/api/kbs/{kb_env['kb_b'].id}/ask", json={'question': '机密内容'})

    assert r.status_code == 404
    assert r.get_json()['error'] == '知识库不存在'


def test_ask_cannot_leak_other_tenant_content(app_with_db, kb_env):
    """🔴 不只是404 —— 响应里绝不能出现 B 的内容。"""

    c = app_with_db.test_client()
    _login(c)

    body = c.post(
        f"/api/kbs/{kb_env['kb_b'].id}/ask",
        json={'question': 'ZZZ 机密'},
    ).get_data(as_text=True)

    assert 'ZZZ' not in body, '🔴 响应里泄漏了 B 租户的内容'


def test_member_can_ask_own_kb(app_with_db, kb_env):
    """成员有ask 权限。"""

    c = app_with_db.test_client()
    _login(c, '甲成员')
    kb_id = kb_env['kb_a'].id
    _upload(c, kb_id)

    r = c.post(f'/api/kbs/{kb_id}/ask', json={'question': '连接池'})

    assert r.status_code == 200


# ============ 4 · 拿掉 key 仍不 500 ============
def test_ask_without_api_key_does_not_500(app_with_db, kb_env, monkeypatch):
    """🔴 无key 时走降级，接口不能 500。"""

    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setenv('RMB01_USE_AGENT', '1')

    c = app_with_db.test_client()
    _login(c)
    kb_id = kb_env['kb_a'].id
    _upload(c, kb_id)

    r = c.post(f'/api/kbs/{kb_id}/ask',
               json={'question': '连接池大小怎么估算', 'use_agent': True})

    assert r.status_code == 200, '🔴 无 key 不能返 500'
    body = r.get_json()
    assert body['degraded'] is True, '无 key 时必须标记降级'
    assert body['degradation_reasons'], '降级要有原因说明'
    assert body['retrieved_count'] >= 1, '降级也要有检索证据'
    assert body['references'], '🔴 即使降级也要给出证据引用'


def test_ask_with_invalid_api_key_does_not_500(app_with_db, kb_env, monkeypatch):
    """无效key 也不能 500。"""

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'invalid-key-for-test')
    monkeypatch.setenv('RMB01_USE_AGENT', '1')

    c = app_with_db.test_client()
    _login(c)
    kb_id = kb_env['kb_a'].id
    _upload(c, kb_id)

    r = c.post(f'/api/kbs/{kb_id}/ask',
               json={'question': '连接池大小怎么估算', 'use_agent': True})

    assert r.status_code == 200
    assert r.get_json()['references']


# ============ 5 · 引用解析回文档 ============
def test_resolve_citations_maps_back_to_document(app_with_db, kb_env):
    """🔴 答案里的 [doc:N] 能解析回具体文档与段落。"""

    from kb_api import resolve_citations

    references = [
        {'document_id': 42, 'title': '运维手册', 'chunk_index': 2},
        {'document_id': 43, 'title': '其他文档', 'chunk_index': 0},
    ]

    resolved = resolve_citations('依据 [doc:42] 与 [doc:43]，见 [doc:999]。', references)

    ids = {r['document_id'] for r in resolved}
    assert ids == {42, 43}, '应解析出 42 与 43'
    assert '999' not in ids, '不存在的 doc 不该被解析出来'
    chunk = next(r for r in resolved if r['document_id'] == 42)
    assert chunk['title'] == '运维手册'
    assert chunk['chunk_index'] == 2


def test_ask_response_includes_resolved_citations(app_with_db, kb_env):
    """接口返回里应带 resolved_citations 字段（即使答案是空的）。"""

    c = app_with_db.test_client()
    _login(c)
    kb_id = kb_env['kb_a'].id
    _upload(c, kb_id)

    r = c.post(f'/api/kbs/{kb_id}/ask', json={'question': '连接池'})

    assert 'resolved_citations' in r.get_json()