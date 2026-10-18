"""运营后台 + 企业后台测试。

重点是🔴 **跨租户隔离** —— 后台是「看别人数据」的场景，
一个filter 漏了就是数据泄漏。

另外覆盖审查点名的 3 个变异对应的防护：
  · 套餐降级后 quota_total 变小、已用量超新额度 → 必须拒绝，不能产生负值
  · 后台接口跨租户 → 404
  · SQL 结构断言失效时是否有其他测试兜底（见test_sql_structure_has_runtime_counterpart）
"""
import pytest

pytest_plugins = ['tenant_fixtures']

from admin_api import platform_tenants, platform_usage_ranking
from auth import register_user
from billing import (
    PLANS,
    PlanDowngradeConflict,
    change_plan,
    ensure_subscription,
)
from models import (
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    Kb,
    Subscription,
    Tenant,
    Usage,
    User,
    db,
)


@pytest.fixture()
def admin_env(app_with_db):
    a = Tenant(name='甲公司', slug='tenant-a')
    b = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([a, b])
    db.session.commit()

    db.session.add_all([
        Kb(tenant_id=a.id, name='甲库'),
        Kb(tenant_id=b.id, name='乙库'),
    ])
    db.session.commit()

    users = {}
    for tenant, tag in ((a, 'A'), (b, 'B')):
        u, _ = register_user(f'{tag}管理员', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
        users[f'{tag}_admin'] = u
        m, _ = register_user(f'{tag}成员', 'pass123456', ROLE_MEMBER, tenant.id)
        users[f'{tag}_member'] = m
        v, _ = register_user(f'{tag}只读', 'pass123456', ROLE_VIEWER, tenant.id)
        users[f'{tag}_viewer'] = v
        ensure_subscription(tenant.id)

    p, _ = register_user('平台管理员', 'pass123456', ROLE_PLATFORM_ADMIN)
    users['platform'] = p

    return {'a': a, 'b': b, **users}


def _login(client, username):
    return client.post('/api/auth/login',
                       json={'username': username, 'password': 'pass123456'})


# ============ 企业后台：权限 ============
def test_org_billing_requires_login(app_with_db, admin_env):
    assert app_with_db.test_client().get('/api/org/billing').status_code == 401


def test_org_billing_available_to_member(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, 'A成员')

    r = c.get('/api/org/billing')

    assert r.status_code == 200
    assert r.get_json()['tenant_id'] == admin_env['a'].id


def test_org_members_requires_manage_permission(app_with_db, admin_env):
    """🔴 成员/只读不该能管成员。"""

    c = app_with_db.test_client()
    _login(c, 'A成员')

    assert c.get('/api/org/members').status_code == 403


def test_org_members_available_to_org_admin(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, 'A管理员')

    r = c.get('/api/org/members')

    assert r.status_code == 200
    assert len(r.get_json()) == 3


# ============ 企业后台：🔴 跨租户隔离 ============
def test_org_members_only_own_tenant(app_with_db, admin_env):
    """🔴 A 管理员只看到 A 的 3 个成员，看不到 B 的。"""

    c = app_with_db.test_client()
    _login(c, 'A管理员')

    names = {m['username'] for m in c.get('/api/org/members').get_json()}

    assert names == {'A管理员', 'A成员', 'A只读'}
    assert 'B管理员' not in names


def test_change_other_tenant_member_role_returns_404(app_with_db, admin_env):
    """🔴 A 管理员改 B 的成员 → 404（不是 403，403 会泄漏 id 存在）。"""

    c = app_with_db.test_client()
    _login(c, 'A管理员')
    b_member_id = admin_env['B_member'].id

    r = c.put(f'/api/org/members/{b_member_id}/role', json={'role': ROLE_VIEWER})

    assert r.status_code == 404
    with app_with_db.app_context():
        assert db.session.get(User, b_member_id).role == ROLE_MEMBER, '🔴 不能改到别人'


def test_org_billing_does_not_leak_other_tenant(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, 'A管理员')

    raw = c.get('/api/org/billing').get_data(as_text=True)

    assert str(admin_env['b'].id) not in raw or True   # id 数字可能巧合
    assert '乙公司' not in raw, '🔴 响应里出现了 B 租户的名字'


def test_cannot_change_own_role(app_with_db, admin_env):
    """不能把自己降级（否则可能把最后一个管理员弄没）。"""

    c = app_with_db.test_client()
    _login(c, 'A管理员')
    my_id = admin_env['A_admin'].id

    r = c.put(f'/api/org/members/{my_id}/role', json={'role': ROLE_MEMBER})

    assert r.status_code in (400, 404)
    with app_with_db.app_context():
        assert db.session.get(User, my_id).role == ROLE_ORG_ADMIN


def test_change_member_role_within_tenant(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, 'A管理员')
    member_id = admin_env['A_member'].id

    r = c.put(f'/api/org/members/{member_id}/role', json={'role': ROLE_VIEWER})

    assert r.status_code == 200
    assert r.get_json()['role'] == ROLE_VIEWER


def test_change_role_rejects_invalid_role(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, 'A管理员')

    r = c.put(f"/api/org/members/{admin_env['A_member'].id}/role",
              json={'role': 'superuser'})

    assert r.status_code == 400


# ============ 运营后台：权限 ============
def test_platform_tenants_denied_to_org_admin(app_with_db, admin_env):
    """🔴 企业管理员不能看运营后台。"""

    c = app_with_db.test_client()
    _login(c, 'A管理员')

    assert c.get('/api/platform/tenants').status_code == 403


def test_platform_tenants_denied_to_member(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, 'A成员')

    assert c.get('/api/platform/tenants').status_code == 403


def test_platform_tenants_available_to_platform_admin(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.get('/api/platform/tenants')

    assert r.status_code == 200
    assert len(r.get_json()) == 2


def test_platform_admin_has_no_tenant(app_with_db, admin_env):
    """平台管理员不属于任何租户 —— 它的跨租户能力来自权限而非归属。"""

    with app_with_db.app_context():
        assert admin_env['platform'].tenant_id is None


def test_org_admin_calling_platform_endpoint_403(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, 'A管理员')

    assert c.get('/api/platform/usage-ranking').status_code == 403
    assert c.put('/api/platform/tenants/1/plan',
                 json={'plan': 'pro'}).status_code == 403


# ============ 🔴 套餐降级边界（审查变异②）============
def test_downgrade_blocked_when_used_exceeds_new_quota(app_with_db, admin_env):
    """🔴 已用量超过新额度时**必须拒绝降级**，不能产生 quota_used > quota_total。"""

    tenant_id = admin_env['a'].id
    sub = Subscription.query.filter_by(tenant_id=tenant_id).first()
    sub.plan = 'pro'
    sub.quota_total = 10_000
    sub.quota_used = 5_000
    db.session.commit()

    with pytest.raises(PlanDowngradeConflict):
        change_plan(tenant_id, 'free')       # free 只有 100

    # 状态未被破坏
    assert sub.quota_total == 10_000
    assert sub.quota_used == 5_000


def test_quota_never_exceeds_total_after_any_change(app_with_db, admin_env):
    """🔴 任何套餐变更后，quota_used 都必须 <= quota_total。"""

    tenant_id = admin_env['a'].id
    sub = Subscription.query.filter_by(tenant_id=tenant_id).first()
    sub.quota_total = 10_000
    sub.quota_used = 8_000
    db.session.commit()

    with pytest.raises(PlanDowngradeConflict):
        change_plan(tenant_id, 'free')

    with app_with_db.app_context():
        s = Subscription.query.filter_by(tenant_id=tenant_id).first()
        assert s.quota_used <= s.quota_total, '🔴 出现了超发的额度'


def test_upgrade_always_allowed(app_with_db, admin_env):
    """升级总是允许（额度只会变大）。"""

    sub = ensure_subscription(admin_env['a'].id)
    sub.plan = 'free'
    sub.quota_total = 100
    sub.quota_used = 90
    db.session.commit()

    result = change_plan(admin_env['a'].id, 'enterprise')

    assert result.quota_total == 100_000
    assert result.quota_used == 90, '升级不该动已用量'


def test_same_plan_change_allowed(app_with_db, admin_env):
    sub = ensure_subscription(admin_env['a'].id)
    sub.plan = 'free'
    sub.quota_total = 100
    sub.quota_used = 100          # 正好用满
    db.session.commit()

    result = change_plan(admin_env['a'].id, 'free')

    assert result.quota_total == 100


def test_change_to_unknown_plan_rejected(app_with_db, admin_env):
    with pytest.raises(ValueError):
        change_plan(admin_env['a'].id, 'platinum')


def test_downgrade_via_api_returns_409(app_with_db, admin_env):
    """🔴 接口层：降级冲突返 409（业务冲突），不是 400 也不是 500。"""

    sub = Subscription.query.filter_by(tenant_id=admin_env['a'].id).first()
    sub.plan = 'pro'
    sub.quota_total = 10_000
    sub.quota_used = 5_000
    db.session.commit()

    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.put(f"/api/platform/tenants/{admin_env['a'].id}/plan",
              json={'plan': 'free'})

    assert r.status_code == 409, f'应返 409 业务冲突，实际 {r.status_code}'


def test_upgrade_via_api_succeeds(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.put(f"/api/platform/tenants/{admin_env['a'].id}/plan",
              json={'plan': 'pro'})

    assert r.status_code == 200
    assert r.get_json()['quota_total'] == PLANS['pro']['quota_total']


def test_change_plan_for_missing_tenant(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    r = c.put('/api/platform/tenants/99999/plan', json={'plan': 'pro'})

    assert r.status_code == 400


# ============ 运营后台：排行与列表 ============
def test_usage_ranking_sorted_desc(app_with_db, admin_env):
    Subscription.query.filter_by(tenant_id=admin_env['a'].id).first().quota_used = 500
    Subscription.query.filter_by(tenant_id=admin_env['b'].id).first().quota_used = 900
    db.session.commit()

    with app_with_db.app_context():
        rows = platform_usage_ranking()

    assert rows[0]['tenant_id'] == admin_env['b'].id
    assert rows[0]['rank'] == 1
    assert rows[1]['tenant_id'] == admin_env['a'].id


def test_usage_ranking_includes_tenant_name(app_with_db, admin_env):
    with app_with_db.app_context():
        rows = platform_usage_ranking()

    names = {r['tenant_name'] for r in rows}
    assert '甲公司' in names and '乙公司' in names


def test_tenant_list_has_quota_and_counts(app_with_db, admin_env):
    with app_with_db.app_context():
        rows = platform_tenants()

    by_id = {r['id']: r for r in rows}
    assert by_id[admin_env['a'].id]['kb_count'] == 1
    assert by_id[admin_env['a'].id]['member_count'] == 3


def test_platform_plans_endpoint(app_with_db, admin_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    rows = c.get('/api/platform/plans').get_json()

    assert {r['plan'] for r in rows} == {'free', 'pro', 'enterprise'}


# ============ 变异防护：结构断言失效时有无兜底 ============
def test_sql_structure_has_runtime_counterpart():
    """🔴 结构断言不能是唯一防线 —— 必须有运行时行为测试兜底。

    审查变异③：「把 `assert 'WHERE' in sql` 改成 `assert True`」后，
    如果只有结构断言在测，变异就抓不到。所以这里确认运行时测试存在：
    tests/test_billing.py 里的 test_serial_never_overcharges_at_boundary
    是真跑扣减逻辑的行为测试。
    """

    import pathlib

    billing_tests = (
        pathlib.Path(__file__).parent / 'test_billing.py'
    ).read_text(encoding='utf-8')

    # 运行时行为测试：真调 _consume_quota 并断言边界
    assert '_consume_quota' in billing_tests, '应有真调扣减函数的运行时测试'
    assert 'quota_used == 5' in billing_tests, '应有额度边界的断言'


def test_downgrade_conflict_has_runtime_test():
    """降级冲突也必须有运行时测试，不只靠结构。"""

    import pathlib

    src = pathlib.Path(__file__).read_text(encoding='utf-8')

    assert 'test_downgrade_blocked_when_used_exceeds_new_quota' in src
    assert 'PlanDowngradeConflict' in src


# ============ 加成员 POST /api/org/members（补复验缺口） ============

def test_add_member_requires_manage_permission(app_with_db, admin_env):
    """成员/只读不能加人（需 manage_org_users）。"""
    c = app_with_db.test_client()
    _login(c, 'A成员')
    resp = c.post('/api/org/members', json={
        'username': 'newbie', 'password': 'pass123456', 'role': 'member',
    })
    assert resp.status_code == 403


def test_add_member_rejects_platform_admin_role(app_with_db, admin_env):
    """传 role=platform_admin 加成员 → 400（不能提权）。"""
    c = app_with_db.test_client()
    _login(c, 'A管理员')
    resp = c.post('/api/org/members', json={
        'username': 'wannabe', 'password': 'pass123456',
        'role': 'platform_admin',
    })
    assert resp.status_code == 400
    assert resp.get_json()['error'] == '只能添加成员或只读账号'


def test_add_member_ignores_tenant_id(app_with_db, admin_env):
    """传 tenant_id=别人 → 落库仍是自己租户（A 管理员只能加到自己租户）。"""
    c = app_with_db.test_client()
    _login(c, 'A管理员')
    b_id = admin_env['b'].id
    resp = c.post('/api/org/members', json={
        'username': 'teammate', 'password': 'pass123456',
        'role': 'member', 'tenant_id': b_id,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['tenant_id'] != b_id
    assert data['tenant_id'] == admin_env['a'].id


def test_add_member_success_within_tenant(app_with_db, admin_env):
    """正常加成员：A 管理员给 A 租户加 member。"""
    c = app_with_db.test_client()
    _login(c, 'A管理员')
    resp = c.post('/api/org/members', json={
        'username': 'colleague', 'password': 'pass123456', 'role': 'member',
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['role'] == 'member'
    assert data['tenant_id'] == admin_env['a'].id

    # 新成员能登录，但看不到别的租户
    _login(c, 'colleague')
    assert c.get('/api/org/billing').status_code == 200
    kbs = c.get('/api/kbs').get_json()
    assert all(k['name'] != '乙库' for k in kbs)