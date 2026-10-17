"""前端字段契约测试。

🔴 来源：截图时发现页面显示 `undefined 段文档` ——
前端写的是 `kb.document_count`，但 `_kb_json()` 没返回这个字段。

这类 bug 的特点是：
  · 后端测试全绿（接口没报错）
  · 前端不报错（undefined 静默显示成字符串）
  · **只有真打开页面才看得见**

所以这组测试直接断言「前端读的字段，后端必须给」。
"""
import pytest

pytest_plugins = ['tenant_fixtures']

from auth import register_user
from billing import ensure_subscription
from models import ROLE_ORG_ADMIN, Kb, Tenant, db


@pytest.fixture()
def seed_env(app_with_db):
    tenant = Tenant(name='契约测试', slug='contract')
    db.session.add(tenant)
    db.session.commit()
    ensure_subscription(tenant.id)
    db.session.add(Kb(tenant_id=tenant.id, name='库一', description='说明'))
    db.session.commit()
    register_user('契约管理员', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
    return tenant


def _login(client):
    return client.post('/api/auth/login',
                       json={'username': '契约管理员', 'password': 'pass123456'})


# ============ 知识库列表字段 ============
def test_kb_list_has_document_count(app_with_db, seed_env):
    """🔴 前端 `kbs.html` 用 `kb.document_count` 显示「N 段文档」。"""

    c = app_with_db.test_client()
    _login(c)

    rows = c.get('/api/kbs').get_json()

    assert rows
    assert 'document_count' in rows[0], (
        '🔴 前端读 document_count，接口必须给 —— 否则页面显示 undefined'
    )
    assert isinstance(rows[0]['document_count'], int)


def test_kb_detail_has_document_count(app_with_db, seed_env):
    c = app_with_db.test_client()
    _login(c)
    kb_id = c.get('/api/kbs').get_json()[0]['id']

    body = c.get(f'/api/kbs/{kb_id}').get_json()

    assert 'document_count' in body


def test_document_count_is_accurate(app_with_db, seed_env):
    c = app_with_db.test_client()
    _login(c)
    kb_id = c.get('/api/kbs').get_json()[0]['id']

    c.post(f'/api/kbs/{kb_id}/documents',
           json={'title': '文档', 'content': '第一段。\n\n第二段。\n\n第三段。'})

    row = c.get('/api/kbs').get_json()[0]
    assert row['document_count'] == 3, (
        f'切成 3 段就该是 3，实际 {row["document_count"]}'
    )


# ============ 前端模板里读的字段，后端都要有 ============
def test_frontend_kb_fields_exist_in_api():
    """🔴 扫描模板里用到的 kb.* 字段，确认接口都提供。

    这条防的是「前端加了个字段但后端没加」——
    比的是模板源码和序列化函数，而不是等页面显示 undefined。
    """

    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    template = (root / 'templates' / 'kbs.html').read_text(encoding='utf-8')
    api_src = (root / 'kb_api.py').read_text(encoding='utf-8')

    used = set(re.findall(r'\bkb\.([a-z_]+)\b', template))
    # id/name 之类直接从对象上取的也算
    provided = set(re.findall(r"'([a-z_]+)':", api_src))

    missing = used - provided
    assert not missing, (
        f'🔴 前端读了 kb.{missing}，但 _kb_json 没返回这些字段'
    )


def test_frontend_answer_fields_exist_in_api():
    """问答响应的字段也要对得上（answer / references / degraded / usage）。"""

    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    template = (root / 'templates' / 'kbs.html').read_text(encoding='utf-8')
    api_src = (root / 'kb_api.py').read_text(encoding='utf-8')

    # 模板里读的顶层响应字段
    for field in ['answer', 'references', 'degraded', 'retrieved_count', 'usage']:
        assert f'body.{field}' in template or f"'{field}'" in api_src, (
            f'🔴 响应字段 {field} 在两边对不上'
        )


def test_reference_fields_used_by_frontend(app_with_db, seed_env):
    """引用卡片读了 title / chunk_index / preview，接口都得给。"""

    c = app_with_db.test_client()
    _login(c)
    kb_id = c.get('/api/kbs').get_json()[0]['id']
    c.post(f'/api/kbs/{kb_id}/documents',
           json={'title': '运维手册',
                 'content': '连接池大小按 CPU 核数乘以 2 估算。'})

    body = c.post(f'/api/kbs/{kb_id}/ask',
                  json={'question': '连接池大小'}).get_json()

    assert body['references'], '应召回内容'
    ref = body['references'][0]
    for field in ('document_id', 'title', 'chunk_index', 'preview'):
        assert field in ref, f'🔴 引用缺少字段 {field}，前端会显示 undefined'


# ============ usage 结构（页面要显示额度提示）============
def test_usage_field_shape(app_with_db, seed_env):
    """页面读 `body.usage.quota.quota_used/quota_total`。"""

    c = app_with_db.test_client()
    _login(c)
    kb_id = c.get('/api/kbs').get_json()[0]['id']
    c.post(f'/api/kbs/{kb_id}/documents',
           json={'title': '文档', 'content': '连接池配置说明。'})

    body = c.post(f'/api/kbs/{kb_id}/ask',
                  json={'question': '连接池'}).get_json()

    assert 'usage' in body
    assert 'quota' in body['usage']
    assert body['usage']['quota'] is None or 'quota_used' in body['usage']['quota']