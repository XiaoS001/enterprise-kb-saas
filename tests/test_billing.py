"""计量与额度测试。

三条硬约束：
  1. 🔴 原子扣减（rowcount）—— 不能「先查后减」
  2. 🔴 额度用完返明确提示，不是 500
  3. 🔴 降级的问答**不扣额度**

外加你要求的**账实相符恒等式**：
  Usage 里被计费的条数 == Subscription.quota_used
"""
import pytest

pytest_plugins = ['tenant_fixtures']

from auth import register_user
from billing import (
    DEFAULT_PLAN,
    PLANS,
    QuotaExceeded,
    ensure_subscription,
    quota_status,
    record_usage,
    usage_summary,
)
from models import (
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    Kb,
    Subscription,
    Tenant,
    Usage,
    db,
)


@pytest.fixture()
def billing_env(app_with_db):
    tenant = Tenant(name='甲公司', slug='tenant-a')
    other = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([tenant, other])
    db.session.commit()

    kb = Kb(tenant_id=tenant.id, name='甲的库')
    db.session.add(kb)
    db.session.commit()

    register_user('甲管理员', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
    register_user('甲成员', 'pass123456', ROLE_MEMBER, tenant.id)
    register_user('乙管理员', 'pass123456', ROLE_ORG_ADMIN, other.id)

    return {'t': tenant, 'other': other, 'kb': kb}


def _set_quota(tenant_id, total):
    sub = ensure_subscription(tenant_id)
    sub.quota_total = total
    sub.quota_used = 0
    db.session.commit()
    return sub


# ============ 1. 原子扣减 ============
def test_single_charge_increments_used(billing_env):
    _set_quota(billing_env['t'].id, 100)

    usage, err = record_usage(billing_env['t'].id, billing_env['kb'].id, '问题1')
    assert err is None
    assert usage is not None
    assert Subscription.query.filter_by(
        tenant_id=billing_env['t'].id
    ).first().quota_used == 1


def test_serial_simulated_concurrency_never_overcharges(billing_env):
    """🔴 额度设 5，串行打 10 次 → 恰好 5 成功 / 5 拒绝 / quota_used == 5。

    用**串行**而不是真起50 线程：SQLite 真并发会锁库，
    测不出原子性只测出锁竞争。串行能精确验证「判定+扣减」的边界。
    """

    _set_quota(billing_env['t'].id, 5)

    succeeded, rejected = 0, 0
    for i in range(10):
        try:
            _usage, err = record_usage(billing_env['t'].id, billing_env['kb'].id, f'问题{i}')
            if err is None:
                succeeded += 1
            else:
                rejected += 1
        except QuotaExceeded:
            rejected += 1

    sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    assert succeeded == 5, '🔴 应恰好成功 5 次'
    assert rejected == 5, '应恰好拒绝 5 次'
    assert sub.quota_used == 5, f'🔴 quota_used 应恰好 5，实际 {sub.quota_used}'


def test_charge_at_exact_boundary(billing_env):
    """🔴 边界：quota_used == quota_total 时必须拒绝（这就是 < 而非 <= 的意义）。"""

    _set_quota(billing_env['t'].id, 3)
    for i in range(3):
        record_usage(billing_env['t'].id, billing_env['kb'].id, f'q{i}')

    sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    assert sub.quota_used == 3

    with pytest.raises(QuotaExceeded):
        record_usage(billing_env['t'].id, billing_env['kb'].id, '第4次')

    assert sub.quota_used == 3, '被拒的请求不该改动计数'


# ============ 2. 额度用完不抛 500 ============
def test_quota_exceeded_raises_dedicated_exception(billing_env):
    _set_quota(billing_env['t'].id, 1)
    record_usage(billing_env['t'].id, billing_env['kb'].id, '第一次')

    with pytest.raises(QuotaExceeded) as exc:
        record_usage(billing_env['t'].id, billing_env['kb'].id, '第二次')

    assert '额度' in str(exc.value)
    assert exc.value.total == 1


def test_quota_exceeded_returns_403_not_500(app_with_db, billing_env, monkeypatch):
    """🔴 接口层：额度用完 → 403 + 明确提示，不是 500。

    三个必要条件（都是设计使然，不是 bug）：
    1. **必须有 key** —— 无 key 是降级问答，按规则不扣额度；
    2. **必须命中检索** —— 检索未命中走早退分支，同样不扣额度；
    3. **必须让 agent 真的产出答案** —— 降级（无论什么原因）都不计费。

    ⚠️ 这里 mock 的是 `kb_api._ask_via_agent` 而不是 agent 内部的 `_call_model`：
       mock 内部模型会让 critic/answerer 在「答案无有效引用」时反复重路由，
       撞 GRAPH_RECURSION_LIMIT —— 那是 agent 状态机的行为，不是计费的。
       计费测试该隔离的是「agent 返回什么」，不该连带测状态机。
    """

    import kb_api

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key-for-billing')
    monkeypatch.setattr(
        kb_api, '_ask_via_agent',
        lambda question, evidence: (f'答案正文。{evidence[:40]}', []),
    )

    c = app_with_db.test_client()
    c.post('/api/auth/login', json={'username': '甲管理员', 'password': 'pass123456'})
    _set_quota(billing_env['t'].id, 1)

    c.post(f"/api/kbs/{billing_env['kb'].id}/documents",
           json={'title': '文档', 'content': '连接池大小应根据 CPU 核数乘以 2 估算。'})

    question = '连接池大小'
    first = c.post(f"/api/kbs/{billing_env['kb'].id}/ask",
                   json={'question': question, 'use_agent': True})
    assert first.status_code == 200, f'第一次应成功：{first.get_json()}'
    first_body = first.get_json()
    assert first_body['degraded'] is False, '有 key + agent 正常返回 → 不该降级'
    assert first_body['usage']['quota']['quota_used'] == 1, '第一次应扣 1 次'

    second = c.post(f"/api/kbs/{billing_env['kb'].id}/ask",
                    json={'question': question, 'use_agent': True})
    assert second.status_code == 403, f'🔴 应是 403，不是 500：{second.get_json()}'
    body = second.get_json()
    assert body['code'] == 'quota_exceeded'
    assert '额度' in body['error']


# ============ 3. 降级不扣额度 ============
def test_degraded_query_does_not_consume_quota(billing_env):
    """🔴 降级的问答不扣额度 —— 这条直接验阶段 4 前置修复的价值。"""

    _set_quota(billing_env['t'].id, 10)

    usage, err = record_usage(
        billing_env['t'].id, billing_env['kb'].id, '降级问答',
        degraded=True,
    )

    assert err is None
    assert usage.degraded is True
    sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    assert sub.quota_used == 0, '🔴 降级不该扣额度'


def test_degraded_still_records_usage_row(billing_env):
    """降级也记流水（degraded=True），便于运营看到降级率。"""

    _set_quota(billing_env['t'].id, 10)
    record_usage(billing_env['t'].id, billing_env['kb'].id, '降级问答', degraded=True)

    rows = Usage.query.filter_by(tenant_id=billing_env['t'].id).all()
    assert len(rows) == 1
    assert rows[0].degraded is True
    assert rows[0].tokens == 0, '降级不该计 token'


def test_api_no_key_query_does_not_consume_quota(app_with_db, billing_env, monkeypatch):
    """🔴 端到端：无 key 的问答（降级）不扣额度。"""

    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setenv('RMB01_USE_AGENT', '1')

    c = app_with_db.test_client()
    # 管理员（member 没有 ask 权限）
    c.post('/api/auth/login', json={'username': '甲管理员', 'password': 'pass123456'})
    _set_quota(billing_env['t'].id, 10)
    c.post(f"/api/kbs/{billing_env['kb'].id}/documents",
           json={'title': '文档', 'content': '连接池配置说明。'})

    for _ in range(3):
        c.post(f"/api/kbs/{billing_env['kb'].id}/ask",
               json={'question': '连接池', 'use_agent': True})

    sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    assert sub.quota_used == 0, '🔴 3 次降级问答不该扣任何额度'


# ============ 4. 账实相符恒等式 ============
def test_usage_count_equals_quota_used(billing_env):
    """🔴 账实相符：被计费的流水数 == quota_used。

    这是计费系统最典型的 bug：两边对不上意味着要么多扣了、
    要么扣了但没记账。
    """

    _set_quota(billing_env['t'].id, 100)
    for i in range(7):
        record_usage(billing_env['t'].id, billing_env['kb'].id, f'正常{i}')
    for i in range(3):
        record_usage(billing_env['t'].id, billing_env['kb'].id, f'降级{i}', degraded=True)

    sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    billable_rows = Usage.query.filter_by(
        tenant_id=billing_env['t'].id, degraded=False,
    ).count()
    all_rows = Usage.query.filter_by(tenant_id=billing_env['t'].id).count()

    assert billable_rows == 7
    assert all_rows == 10, '降级的 3 条也要记流水'
    assert sub.quota_used == billable_rows, (
        f'🔴 账实不符：quota_used={sub.quota_used} '
        f'但被计费流水={billable_rows} 条'
    )


def test_rejected_requests_do_not_create_usage_rows(billing_env):
    """🔴 被额度拒绝的请求不该留下流水（否则流水比 quota_used 多）。"""

    _set_quota(billing_env['t'].id, 2)
    record_usage(billing_env['t'].id, billing_env['kb'].id, 'ok1')
    record_usage(billing_env['t'].id, billing_env['kb'].id, 'ok2')

    with pytest.raises(QuotaExceeded):
        record_usage(billing_env['t'].id, billing_env['kb'].id, '被拒')

    assert Usage.query.filter_by(tenant_id=billing_env['t'].id).count() == 2
    sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    assert sub.quota_used == 2


# ============ 5. 租户隔离 ============
def test_charge_only_affects_own_tenant(billing_env):
    """🔴 A 的问答不能扣 B 的额度。"""

    _set_quota(billing_env['t'].id, 10)
    _set_quota(billing_env['other'].id, 10)

    for i in range(3):
        record_usage(billing_env['t'].id, billing_env['kb'].id, f'A{i}')

    a_sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    b_sub = Subscription.query.filter_by(tenant_id=billing_env['other'].id).first()
    assert a_sub.quota_used == 3
    assert b_sub.quota_used == 0, '🔴 B 的额度不该被动'


def test_cross_tenant_charge_uses_own_subscription(billing_env):
    """用 B 的 tenant_id 调用时，扣的必须是 B 自己的额度。"""

    _set_quota(billing_env['t'].id, 5)
    _set_quota(billing_env['other'].id, 5)

    record_usage(billing_env['other'].id, None, 'B的问答')

    b_sub = Subscription.query.filter_by(tenant_id=billing_env['other'].id).first()
    a_sub = Subscription.query.filter_by(tenant_id=billing_env['t'].id).first()
    assert b_sub.quota_used == 1
    assert a_sub.quota_used == 0


# ============ 6. 套餐 ============
def test_three_plans_defined():
    assert set(PLANS) == {'free', 'pro', 'enterprise'}


def test_plans_have_increasing_quota():
    quotas = [PLANS[p]['quota_total'] for p in ('free', 'pro', 'enterprise')]
    assert quotas == sorted(quotas), '额度应随套餐递增'


def test_ensure_subscription_is_idempotent(billing_env):
    first = ensure_subscription(billing_env['t'].id)
    second = ensure_subscription(billing_env['t'].id)
    assert first.id == second.id
    assert Subscription.query.filter_by(tenant_id=billing_env['t'].id).count() == 1


def test_default_plan_is_free(billing_env):
    sub = ensure_subscription(billing_env['t'].id)
    assert sub.plan == DEFAULT_PLAN


def test_quota_status_reports_remaining(billing_env):
    _set_quota(billing_env['t'].id, 10)
    record_usage(billing_env['t'].id, billing_env['kb'].id, 'q1')

    status = quota_status(billing_env['t'].id)

    assert status['quota_total'] == 10
    assert status['quota_used'] == 1
    assert status['quota_remaining'] == 9
    assert status['exhausted'] is False


def test_quota_status_reports_exhausted(billing_env):
    _set_quota(billing_env['t'].id, 1)
    record_usage(billing_env['t'].id, billing_env['kb'].id, 'q1')

    assert quota_status(billing_env['t'].id)['exhausted'] is True


def test_usage_summary_separates_billable(billing_env):
    _set_quota(billing_env['t'].id, 100)
    record_usage(billing_env['t'].id, billing_env['kb'].id, 'n1')
    record_usage(billing_env['t'].id, billing_env['kb'].id, 'n2', tokens=100)
    record_usage(billing_env['t'].id, billing_env['kb'].id, 'd1', degraded=True)

    summary = usage_summary(billing_env['t'].id)

    assert summary['total_queries'] == 3
    assert summary['billable_queries'] == 2
    assert summary['degraded_queries'] == 1
    assert summary['total_tokens'] == 100


def test_record_usage_without_subscription_errors(billing_env):
    """没有订阅时报错而不是静默。"""

    Subscription.query.filter_by(tenant_id=billing_env['t'].id).delete()
    db.session.commit()

    usage, err = record_usage(billing_env['t'].id, None, '问题')

    assert usage is None
    assert '订阅' in err