"""2026-10-21 维护轮次新增能力的回归测试。

覆盖这一轮加的接口与页面：
  · 企业后台：移除成员 / 用量明细分页
  · 平台后台：单租户画像
  · 检索对比接口（线上口径 vs 对照口径，负样本必须 0 命中）
  · 改密码
  · 新增页面路由与门控（A1 分段预览 / A2 对比 / A3 用量 / A4 租户详情 / A6 个人中心）
  · P1-1 agent.py 不再用「题库」措辞
  · P1-3 run_mutations.py 只在临时副本里变异

🔴 不修改任何既有测试；本文件只做加法。
"""
import pathlib

import pytest

pytest_plugins = ['tenant_fixtures']

from auth import register_user
from billing import ensure_subscription
from models import (
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    Document,
    Kb,
    Tenant,
    User,
    db,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent

DOC_ROWS = [
    ('运维手册', '数据库连接超时通常是连接池耗尽，先看 max_connections 和慢查询。\n'),
    ('运维手册', '连接池大小按并发量配置，一般 CPU 核数的两到四倍。\n'),
    ('产品FAQ', '额度按计费问答次数扣减，降级回答不扣额度。\n'),
]


@pytest.fixture()
def m_env(app_with_db):
    a = Tenant(name='甲公司', slug='tenant-a')
    b = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([a, b])
    db.session.commit()

    kb_a = Kb(tenant_id=a.id, name='甲库')
    kb_b = Kb(tenant_id=b.id, name='乙库')
    db.session.add_all([kb_a, kb_b])
    db.session.commit()

    for title, content in DOC_ROWS:
        db.session.add(Document(tenant_id=a.id, kb_id=kb_a.id,
                                title=title, content=content))
    db.session.commit()

    ensure_subscription(a.id)
    ensure_subscription(b.id)

    accounts = {}
    for tenant, tag in ((a, 'A'), (b, 'B')):
        for role, suffix in ((ROLE_ORG_ADMIN, 'admin'),
                             (ROLE_MEMBER, 'member'),
                             (ROLE_VIEWER, 'viewer')):
            user, _ = register_user(f'{tag}{suffix}', 'pass123456', role, tenant.id)
            accounts[f'{tag}_{suffix}'] = user
    platform, _ = register_user('平台管理员', 'pass123456', ROLE_PLATFORM_ADMIN)
    accounts['platform'] = platform

    return {'a': a, 'b': b, 'kb_a': kb_a, 'kb_b': kb_b, **accounts}


def _login(client, username):
    return client.post('/api/auth/login',
                       json={'username': username, 'password': 'pass123456'})


# ============ 企业后台：移除成员 ============
def test_delete_member_removes_from_tenant(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')
    target = m_env['A_viewer']

    r = c.delete(f'/api/org/members/{target.id}')

    assert r.status_code == 200
    assert User.query.filter_by(id=target.id).first() is None


def test_delete_self_is_rejected(app_with_db, m_env):
    """🔴 删掉自己就再也登不进来，只能找平台管理员救 —— 必须 400。"""

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    r = c.delete(f"/api/org/members/{m_env['A_admin'].id}")

    assert r.status_code == 400
    assert User.query.filter_by(id=m_env['A_admin'].id).first() is not None


def test_delete_cross_tenant_member_returns_404(app_with_db, m_env):
    """🔴 跨租户 → 404（403 会泄漏「这个 id 存在」）。"""

    c = app_with_db.test_client()
    _login(c, 'Aadmin')
    target = m_env['B_viewer']

    r = c.delete(f'/api/org/members/{target.id}')

    assert r.status_code == 404
    assert User.query.filter_by(id=target.id).first() is not None


def test_delete_member_requires_manage_permission(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Amember')

    assert c.delete(f"/api/org/members/{m_env['A_viewer'].id}").status_code == 403


# ============ 企业后台：用量明细 ============
def test_org_usage_detail_only_own_tenant(app_with_db, m_env):
    from models import Usage

    db.session.add_all([
        Usage(tenant_id=m_env['a'].id, user_id=m_env['A_admin'].id,
              kb_id=m_env['kb_a'].id, question='甲的问题', tokens=3, degraded=False),
        Usage(tenant_id=m_env['b'].id, user_id=m_env['B_admin'].id,
              kb_id=m_env['kb_b'].id, question='乙的问题', tokens=3, degraded=False),
    ])
    db.session.commit()

    c = app_with_db.test_client()
    _login(c, 'Aadmin')
    body = c.get('/api/org/usage').get_json()

    assert [item['question'] for item in body['items']] == ['甲的问题']
    assert body['total'] == 1


def test_org_usage_page_size_is_clamped(app_with_db, m_env):
    """🔴 page_size=10**9 不能真把整表捞出来。"""

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    body = c.get('/api/org/usage?page_size=100000').get_json()

    assert body['page_size'] == 100


# ============ 平台后台：单租户画像 ============
def test_platform_tenant_detail_shape(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    body = c.get(f"/api/platform/tenants/{m_env['a'].id}").get_json()

    assert body['name'] == '甲公司'
    assert body['counts']['members'] == 3
    assert body['counts']['documents'] == 3
    assert len(body['daily']) == 7
    assert {kb['name'] for kb in body['kbs']} == {'甲库'}


def test_platform_tenant_detail_missing_returns_404(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    assert c.get('/api/platform/tenants/9999').status_code == 404


def test_platform_tenant_detail_denied_for_org_admin(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    assert c.get(f"/api/platform/tenants/{m_env['a'].id}").status_code == 403


# ============ 检索对比 ============
def test_compare_retrieval_returns_two_strategies(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    r = c.post('/api/compare/retrieval',
               json={'kb_id': m_env['kb_a'].id, 'question': '连接池大小怎么配'})

    assert r.status_code == 200
    body = r.get_json()
    assert set(body['strategies']) == {'strict', 'baseline'}
    assert body['strategies']['strict']['name'] == '线上口径'
    assert body['rows']
    row = body['rows'][0]
    assert isinstance(row['strict']['hits'], list)
    assert isinstance(row['baseline']['hits'], list)
    assert row['baseline']['count'] >= row['strict']['count']


def test_compare_retrieval_negative_probe_hits_nothing(app_with_db, m_env):
    """🔴 负样本：线上口径必须 0 命中，对照口径才会把噪声捞出来。"""

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    body = c.post('/api/compare/retrieval',
                  json={'kb_id': m_env['kb_a'].id,
                        'question': '公司年会在哪办'}).get_json()

    assert body['rows'][0]['strict']['hits'] == []


def test_compare_retrieval_cross_tenant_kb_returns_404(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    r = c.post('/api/compare/retrieval', json={'kb_id': m_env['kb_b'].id})

    assert r.status_code == 404


def test_compare_retrieval_denied_for_platform_admin(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    assert c.post('/api/compare/retrieval',
                  json={'kb_id': m_env['kb_a'].id}).status_code == 403


def test_compare_page_renders(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    r = c.get('/compare')

    assert r.status_code == 200
    assert '检索对比'.encode() in r.data


# ============ 改密码 ============
def test_change_password_roundtrip(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Amember')

    r = c.post('/api/auth/change-password',
               json={'old_password': 'pass123456', 'new_password': 'newpass123'})
    assert r.status_code == 200

    c.post('/api/auth/logout')
    assert c.post('/api/auth/login',
                  json={'username': 'Amember',
                        'password': 'pass123456'}).status_code == 401
    assert c.post('/api/auth/login',
                  json={'username': 'Amember',
                        'password': 'newpass123'}).status_code == 200


def test_change_password_wrong_old_password_rejected(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Amember')

    r = c.post('/api/auth/change-password',
               json={'old_password': 'not-my-password', 'new_password': 'newpass123'})

    assert r.status_code == 400


# ============ 新增页面：路由 + 门控 ============
def test_kb_detail_page_renders_for_own_kb(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    assert c.get(f"/kbs/{m_env['kb_a'].id}").status_code == 200


def test_kb_detail_page_hides_other_tenant_kb(app_with_db, m_env):
    """🔴 别人的库 → 404，不是 403。"""

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    assert c.get(f"/kbs/{m_env['kb_b'].id}").status_code == 404


def test_org_usage_page_requires_manage_permission(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Amember')

    assert c.get('/org/usage').status_code == 403


def test_org_usage_page_renders_for_admin(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    assert c.get('/org/usage').status_code == 200


def test_tenant_detail_page_renders_for_platform(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.get(f"/api/platform/tenants/{m_env['a'].id}")
    assert r.status_code == 200
    assert c.get(f"/platform/tenants/{m_env['a'].id}").status_code == 200


def test_me_page_renders_for_every_role(app_with_db, m_env):
    for name in ('Aadmin', 'Amember', 'Aviewer', '平台管理员'):
        c = app_with_db.test_client()
        _login(c, name)
        assert c.get('/me').status_code == 200, name
        c.post('/api/auth/logout')


def test_profile_api_exposes_own_identity(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Aviewer')

    body = c.get('/api/auth/profile').get_json()

    assert body['username'] == 'Aviewer'
    assert body['tenant_name'] == '甲公司'
    assert body['permissions'] == ['view_history']


# ============ 平台账号（无租户）不该撞 500 ============
def test_tenantless_kb_api_returns_400_not_500(app_with_db, m_env):
    """🔴 平台管理员调 /api/kbs：隔离层会抛 TenantIsolationError，
    必须拦成一句人话 400 —— 500 + 堆栈对演示是灾难。"""

    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.get('/api/kbs')

    assert r.status_code == 400
    assert '租户' in r.get_json()['error']


def test_tenantless_kbs_page_redirects_to_platform(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.get('/kbs')

    assert r.status_code == 302
    assert r.headers['Location'].endswith('/platform')


def test_tenantless_login_redirects_to_platform(app_with_db, m_env):
    """已登录的平台账号打开 /login 不该被送去 /kbs（会再弹回来）。"""

    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.get('/login')

    assert r.status_code == 302
    assert r.headers['Location'].endswith('/platform')


def test_denied_page_still_renders_for_member(app_with_db, m_env):
    c = app_with_db.test_client()
    _login(c, 'Amember')

    r = c.get('/org')

    assert r.status_code == 403
    assert '无权访问'.encode() in r.data


# ============ P1-1 / P1-3 的静态护栏 ============
def test_agent_module_uses_kb_wording():
    """🔴 agent.py 里不该再出现题库术语（qa.py / qa_lc.py 是历史模块，不动）。"""

    source = (ROOT / 'agent.py').read_text(encoding='utf-8')

    assert '没有找到相关题目材料' not in source
    assert 'def search_documents(' in source
    # 技能注册名保留（skill_counts 里被测试断言）
    assert 'registry.get("search_questions")' in source


def test_run_mutations_works_on_temp_copy():
    """🔴 变异测试不能再直接改真实源码。"""

    source = (ROOT / 'run_mutations.py').read_text(encoding='utf-8')

    assert 'def make_workspace(' in source
    assert 'tempfile.mkdtemp(prefix=\'rmb01-mut-\')' in source
    # 还原写的是副本里的那个 path（path 已被重指向 work/...）
    assert 'path.write_text(original_source, encoding=\'utf-8\')' in source


# ============ P0-1 · 规则兜底答案（B 方案） ============
def test_rule_path_returns_structured_fallback(app_with_db, m_env):
    """没有模型时不能给空白页：把命中的原文拼出来，且标明未经模型总结。"""

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    body = c.post(f"/api/kbs/{m_env['kb_a'].id}/ask",
                  json={'question': '连接池大小'}).get_json()

    assert body['retrieved_count'] >= 1, '先要有命中，兜底才有内容'
    assert body['degraded'] is True
    assert body['fallback'] is True
    assert body['answer'].startswith('【未启用模型'), body['answer'][:40]
    # 🔴 前端 renderAnswer 只认 [doc:N]，写成 [doc:N#k] 就成死文本了
    assert '[doc:' in body['answer'] and '#' not in body['answer'].split('\n')[0]
    assert '《' in body['answer'], '要带文档标题，段落才有出处'
    assert body['usage']['billable'] is False, '降级不许扣额度'
    assert body['references'], '兜底也要给引用'


def test_fallback_quota_is_not_charged(app_with_db, m_env):
    """规则兜底的问答不能吃额度（与真调模型区分开）。"""

    from billing import quota_status

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    before = quota_status(m_env['a'].id)['quota_used']
    for _ in range(3):
        c.post(f"/api/kbs/{m_env['kb_a'].id}/ask", json={'question': '连接池大小'})
    after = quota_status(m_env['a'].id)['quota_used']

    assert after == before, '降级问答扣了额度 —— 计费口径破了'


def test_agent_failure_falls_back_to_evidence(app_with_db, m_env, monkeypatch):
    """模型挂了（key 错/模型下线）也要有正文，原因写清楚。"""

    import kb_api

    monkeypatch.setattr(kb_api, '_ask_via_agent',
                        lambda question, evidence: ('', ['模型调用失败: 401 无效密钥']))

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    body = c.post(f"/api/kbs/{m_env['kb_a'].id}/ask",
                  json={'question': '连接池大小', 'use_agent': True}).get_json()

    assert body['degraded'] is True
    assert body['fallback'] is True
    assert body['answer'].startswith('【模型没有给出答案')
    assert any('规则兜底' in reason for reason in body['degradation_reasons'])
    assert any('401' in reason for reason in body['degradation_reasons']), '真实原因不能吞掉'
    assert body['usage']['billable'] is False


def test_agent_success_is_not_labelled_fallback(app_with_db, m_env, monkeypatch):
    """模型真答上了就不能再打「兜底」标 —— 不然等于自己说假话。"""

    import kb_api

    monkeypatch.setattr(kb_api, '_ask_via_agent',
                        lambda question, evidence: ('连接池按核数配 [doc:1]', []))

    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    body = c.post(f"/api/kbs/{m_env['kb_a'].id}/ask",
                  json={'question': '连接池大小', 'use_agent': True}).get_json()

    assert body['degraded'] is False
    assert body['fallback'] is False
    assert body['answer'] == '连接池按核数配 [doc:1]'
    assert body['usage']['billable'] is True
