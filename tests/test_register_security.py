pytest_plugins = ['tenant_fixtures']
"""🔴 注册接口提权防护测试（WorkBuddy 审查发现，2026-10-18）。

修复目标：POST /api/auth/register 只能「新建租户 + 该租户第一个 org_admin」。
请求体里的 role / tenant_id 一律忽略 —— 任何人都不能：
  1. 把自己注册成 platform_admin（提权）
  2. 加入一个已有租户（越权看别人数据）
"""
import pytest

from models import ROLE_ORG_ADMIN, ROLE_PLATFORM_ADMIN, Tenant, User, db


def _register(client, **payload):
    return client.post('/api/auth/register', json=payload)


def test_register_ignores_role_platform_admin(app_with_db):
    """传 platform_admin → 落库仍是 org_admin（不能提权）。"""
    with app_with_db.test_client() as client:
        resp = _register(client, username='attacker', password='123456',
                         role='platform_admin')
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['role'] == ROLE_ORG_ADMIN, f'应强制 org_admin，实际 {data["role"]}'
        assert data['tenant_id'] is not None, '平台管理员无租户；普通注册必须落在租户里'


def test_register_ignores_tenant_id(app_with_db):
    """传别人的 tenant_id → 落库是新建的租户（不能加入已有租户）。"""
    with app_with_db.app_context():
        victim = Tenant(name='受害公司', slug='victim')
        db.session.add(victim)
        db.session.commit()
        victim_id = victim.id   # 先取出，避免 session 关闭后访问 DetachedInstance

    with app_with_db.test_client() as client:
        resp = _register(client, username='intruder', password='123456',
                         role='org_admin', tenant_id=victim_id)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['tenant_id'] != victim_id, '不能加入已有租户，应新建租户'

    with app_with_db.app_context():
        # 入侵者落在自己的新租户，不是受害租户
        intruder = User.query.filter_by(username='intruder').first()
        assert intruder is not None
        assert intruder.tenant_id != victim_id
        assert intruder.role == ROLE_ORG_ADMIN


def test_anonymous_cannot_join_existing_tenant(app_with_db):
    """匿名注册不能加入已有租户 —— 无论传什么 tenant_id。"""
    with app_with_db.app_context():
        victim = Tenant(name='目标公司', slug='target')
        db.session.add(victim)
        db.session.commit()
        victim_id = victim.id

    with app_with_db.test_client() as client:
        resp = _register(client, username='anon', password='123456',
                         tenant_id=victim_id)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['tenant_id'] != victim_id

    with app_with_db.app_context():
        anon = User.query.filter_by(username='anon').first()
        assert anon.tenant_id != victim_id


def test_platform_admin_cannot_be_created_via_api(app_with_db):
    """platform_admin 唯一来源是 seed/CLI，API 注册一律建不出。"""
    with app_with_db.test_client() as client:
        resp = _register(client, username='root_wannabe', password='123456',
                         role='platform_admin', tenant_id=None)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['role'] != ROLE_PLATFORM_ADMIN

    with app_with_db.app_context():
        users = User.query.filter_by(role=ROLE_PLATFORM_ADMIN).all()
        assert users == [], 'API 注册绝不能产生 platform_admin'


def test_register_creates_new_tenant_and_org_admin(app_with_db):
    """正常注册：新建租户 + org_admin，且租户有默认订阅。"""
    with app_with_db.test_client() as client:
        resp = _register(client, username='alice', password='123456',
                         tenant_name='甲公司')
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['role'] == ROLE_ORG_ADMIN

    with app_with_db.app_context():
        tenant = Tenant.query.filter_by(name='甲公司').first()
        assert tenant is not None
        alice = User.query.filter_by(username='alice').first()
        assert alice.tenant_id == tenant.id
        from models import Subscription
        sub = Subscription.query.filter_by(tenant_id=tenant.id).first()
        assert sub is not None, '新租户应有默认订阅'
