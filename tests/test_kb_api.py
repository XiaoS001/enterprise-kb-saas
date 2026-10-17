"""知识库接口测试 + 跨租户隔离（阶段 3）。

🔴 每个返回租户数据的接口都有配套跨租户测试（复验意见第三节要求）：

    GET  /api/kbs                  A 的 token -> 只返回 A 的库
    GET  /api/kbs/<id>             A 请求 B 的 kb_id -> 404
    POST /api/kbs                  请求体塞 tenant_id=B -> 落库仍是 A
    POST /api/kbs/<id>/documents   A 往 B 的库上传 -> 404
    GET  /api/kbs/<id>/documents   A 列 B 的文档 -> 404
"""
import pytest

pytest_plugins = ['tenant_fixtures']

import config
from auth import register_user
from models import (
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    Document,
    Kb,
    Subscription,
    Tenant,
    User,
    db,
)


# ============ fixtures ============

@pytest.fixture()
def two_tenants(app_with_db):
    """两个租户，各有一个库与一份文档，各有一个企业管理员。"""

    a = Tenant(name='甲公司', slug='tenant-a')
    b = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([a, b])
    db.session.commit()

    kb_a = Kb(tenant_id=a.id, name='甲的知识库')
    kb_b = Kb(tenant_id=b.id, name='乙的知识库')
    db.session.add_all([kb_a, kb_b])
    db.session.commit()

    db.session.add_all([
        Document(tenant_id=a.id, kb_id=kb_a.id, title='甲文档',
                 content='甲的内容', chunk_index=0),
        Document(tenant_id=b.id, kb_id=kb_b.id, title='乙文档',
                 content='乙的内容', chunk_index=0),
    ])
    db.session.commit()

    user_a, _ = register_user('甲管理员', 'pass123456', ROLE_ORG_ADMIN, a.id)
    user_b, _ = register_user('乙管理员', 'pass123456', ROLE_ORG_ADMIN, b.id)

    return {
        'a': a, 'b': b,
        'kb_a': kb_a, 'kb_b': kb_b,
        'user_a': user_a, 'user_b': user_b,
    }


def _login(client, username, password='pass123456'):
    return client.post('/api/auth/login', json={
        'username': username, 'password': password,
    })


# ============ GET /api/kbs ============
def test_list_kbs_requires_login(app_with_db):
    assert app_with_db.test_client().get('/api/kbs').status_code == 401


def test_list_kbs_only_returns_own_tenant(app_with_db, two_tenants):
    """🔴 A 登录 -> 只看到 A 的库。"""

    c = app_with_db.test_client()
    _login(c, '甲管理员')

    rows = c.get('/api/kbs').get_json()

    assert len(rows) == 1, '🔴 只能看到自己的库'
    assert rows[0]['name'] == '甲的知识库'


def test_list_kbs_other_tenant_sees_nothing_of_a(app_with_db, two_tenants):
    """🔴 B 登录 -> 看不到 A 的库。"""

    c = app_with_db.test_client()
    _login(c, '乙管理员')

    rows = c.get('/api/kbs').get_json()

    assert len(rows) == 1
    assert rows[0]['name'] == '乙的知识库'


# ============ GET /api/kbs/<id> ============
def test_get_own_kb_ok(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.get(f"/api/kbs/{two_tenants['kb_a'].id}")

    assert r.status_code == 200
    assert r.get_json()['name'] == '甲的知识库'


def test_get_other_tenant_kb_returns_404(app_with_db, two_tenants):
    """🔴 A 请求 B 的 kb_id -> 必须是 404（不是 403，403 会泄漏存在性）。"""

    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.get(f"/api/kbs/{two_tenants['kb_b'].id}")

    assert r.status_code == 404, '🔴 跨租户必须 404'
    assert r.get_json()['error'] == '知识库不存在'


def test_get_missing_kb_returns_404(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    assert c.get('/api/kbs/99999').status_code == 404


# ============ POST /api/kbs（写侧隔离）============
def test_create_kb_lands_in_own_tenant(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.post('/api/kbs', json={'name': '新库'})

    assert r.status_code == 201
    kb_id = r.get_json()['id']
    with app_with_db.app_context():
        assert db.session.get(Kb, kb_id).tenant_id == two_tenants['a'].id


def test_create_kb_ignores_forged_tenant_id(app_with_db, two_tenants):
    """🔴 请求体里塞 tenant_id=B -> 落库仍是 A 自己的。"""

    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.post('/api/kbs', json={
        'name': '伪造的库',
        'tenant_id': two_tenants['b'].id,
    })

    assert r.status_code == 201
    kb_id = r.get_json()['id']
    with app_with_db.app_context():
        kb = db.session.get(Kb, kb_id)
        assert kb.tenant_id == two_tenants['a'].id, '🔴 不能被请求体里的租户覆盖'
        # B 的库里不该出现这个
        from tenancy import scoped
        assert scoped(Kb, two_tenants['b'].id).filter_by(id=kb_id).first() is None


def test_create_kb_requires_name(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.post('/api/kbs', json={'name': '   '})

    assert r.status_code == 400


def test_create_kb_requires_permission(app_with_db, two_tenants):
    """成员不能建库 -> 403。"""

    register_user('甲成员', 'pass123456', ROLE_MEMBER, two_tenants['a'].id)
    c = app_with_db.test_client()
    _login(c, '甲成员')

    r = c.post('/api/kbs', json={'name': '成员建的'})

    assert r.status_code == 403


# ============ POST /api/kbs/<id>/documents ============
def test_upload_document_to_own_kb(app_with_db, two_tenants):
    """上传 -> 分段 -> 落库。"""

    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.post(f"/api/kbs/{two_tenants['kb_a'].id}/documents", json={
        'title': '上传的文档',
        'content': '第一段内容。\n\n第二段内容。\n\n第三段内容。',
    })

    assert r.status_code == 201
    chunks = r.get_json()['chunks']
    assert len(chunks) == 3, '空行分隔应切成 3 段'
    assert chunks[0]['chunk_index'] == 0
    assert chunks[0]['title'] == '上传的文档'


def test_upload_document_to_foreign_kb_returns_404(app_with_db, two_tenants):
    """🔴 A 往 B 的库上传 -> 404。"""

    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.post(f"/api/kbs/{two_tenants['kb_b'].id}/documents", json={
        'title': '偷渡', 'content': 'x',
    })

    assert r.status_code == 404
    with app_with_db.app_context():
        # 确认真的没写进去
        from tenancy import scoped
        assert scoped(Document, two_tenants['b'].id).filter_by(
            title='偷渡'
        ).first() is None


def test_upload_requires_title(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.post(f"/api/kbs/{two_tenants['kb_a'].id}/documents", json={
        'title': '', 'content': 'x',
    })

    assert r.status_code == 400


def test_upload_requires_content(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.post(f"/api/kbs/{two_tenants['kb_a'].id}/documents", json={
        'title': '空文档', 'content': '   ',
    })

    assert r.status_code == 400


# ============ GET /api/kbs/<id>/documents ============
def test_list_own_documents(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.get(f"/api/kbs/{two_tenants['kb_a'].id}/documents")

    assert r.status_code == 200
    rows = r.get_json()
    assert len(rows) == 1
    assert rows[0]['title'] == '甲文档'


def test_list_foreign_kb_documents_returns_404(app_with_db, two_tenants):
    """🔴 A 列 B 的文档 -> 404。"""

    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.get(f"/api/kbs/{two_tenants['kb_b'].id}/documents")

    assert r.status_code == 404


# ============ DELETE ============
def test_delete_own_kb(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.delete(f"/api/kbs/{two_tenants['kb_a'].id}")

    assert r.status_code == 200
    with app_with_db.app_context():
        assert db.session.get(Kb, two_tenants['kb_a'].id) is None


def test_delete_foreign_kb_returns_404(app_with_db, two_tenants):
    """🔴 A 删 B 的库 -> 404，且 B 的库还在。"""

    c = app_with_db.test_client()
    _login(c, '甲管理员')

    r = c.delete(f"/api/kbs/{two_tenants['kb_b'].id}")

    assert r.status_code == 404
    with app_with_db.app_context():
        assert db.session.get(Kb, two_tenants['kb_b'].id) is not None, 'B 的库不能被删'


def test_delete_foreign_document_returns_404(app_with_db, two_tenants):
    c = app_with_db.test_client()
    _login(c, '甲管理员')
    with app_with_db.app_context():
        b_doc_id = Document.query.filter_by(
            tenant_id=two_tenants['b'].id
        ).first().id

    r = c.delete(f'/api/documents/{b_doc_id}')

    assert r.status_code == 404


# ============ /healthz ============
def test_healthz_does_not_leak_secrets(app_with_db):
    r = app_with_db.test_client().get('/healthz')

    assert r.status_code == 200
    body = r.get_json()
    assert body['status'] == 'ok'
    assert 'ai_key_present' in body['config']
    raw = r.get_data(as_text=True)
    assert 'DEEPSEEK_API_KEY' not in raw
    assert 'sk-' not in raw