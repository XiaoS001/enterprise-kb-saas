pytest_plugins = ['tenant_fixtures']
"""认证与四档角色测试。

安全底线：
  · 密码只存 PBKDF2 hash（每用户随机盐）
  · 常量时间比较
  · **不区分「用户不存在」与「密码错」** → 防用户名枚举
  · 登录失败 401，不是 500
"""
import pytest

from auth import (
    PERMISSIONS,
    authenticate,
    has_permission,
    hash_password,
    register_user,
    verify_password,
)
from models import (
    ROLE_LABELS,
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    ALL_ROLES,
    Tenant,
    User,
    db,
)


# app / svc_ctx 由 tenant_fixtures.py 提供（pytest_plugins 挂载），
# 避开 XM03 自带 conftest.py 里的 client fixture 撞名


@pytest.fixture()
def tenant(svc_ctx):
    t = Tenant(name='甲公司', slug='tenant-a')
    db.session.add(t)
    db.session.commit()
    return t


# ============ 密码 hash ============
def test_password_hash_is_not_plaintext():
    stored = hash_password('my-secret-password')
    assert 'my-secret-password' not in stored
    assert stored.startswith('pbkdf2_sha256$')
    assert len(stored.split('$')) == 4


def test_same_password_different_salt_differs():
    """🔴 每用户随机盐 → 同密码不同用户 hash 不同 → 彩虹表无效。"""

    assert hash_password('same', salt='salt-a') != hash_password('same', salt='salt-b')


def test_verify_password_accepts_correct():
    stored = hash_password('correct-password')
    assert verify_password('correct-password', stored) is True


def test_verify_password_rejects_wrong():
    stored = hash_password('correct-password')
    assert verify_password('wrong', stored) is False


@pytest.mark.parametrize('broken', ['', 'not-a-hash', 'a$b$c', None, 'x$y$z$w$e'])
def test_verify_password_handles_malformed(broken):
    """库里存了脏数据也不能抛。"""

    assert verify_password('x', broken) is False


def test_registered_hash_differs_from_password(svc_ctx, tenant):
    user, err = register_user('alice', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
    assert err is None
    assert user.password_hash != 'pass123456'
    assert 'pass123456' not in user.password_hash


# ============ 注册校验 ============
def test_register_rejects_duplicate_username(svc_ctx, tenant):
    register_user('dup', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
    user, err = register_user('dup', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
    assert user is None
    assert '已存在' in err


def test_register_rejects_short_password(svc_ctx, tenant):
    user, err = register_user('user1', '123', ROLE_ORG_ADMIN, tenant.id)
    assert user is None
    assert '密码' in err  # 用户名合法，所以错因确实是密码


def test_register_rejects_short_username(svc_ctx, tenant):
    user, err = register_user('ab', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
    assert user is None
    assert '用户名' in err


def test_register_rejects_invalid_role(svc_ctx, tenant):
    user, err = register_user('validname2', 'pass123456', 'superuser', tenant.id)
    assert user is None
    assert '角色' in err


def test_non_platform_admin_requires_tenant(svc_ctx):
    user, err = register_user('validname3', 'pass123456', ROLE_MEMBER, None)
    assert user is None
    assert 'tenant_id' in err


def test_platform_admin_has_no_tenant(svc_ctx):
    """🔴 平台管理员是跨租户角色，不属于任何租户。"""

    user, err = register_user('root', 'pass123456', ROLE_PLATFORM_ADMIN)
    assert err is None
    assert user.tenant_id is None


# ============ 登录 ============
def test_login_with_correct_credentials(svc_ctx, tenant):
    register_user('bob', 'pass123456', ROLE_MEMBER, tenant.id)
    user, err = authenticate('bob', 'pass123456')
    assert err is None
    assert user.username == 'bob'


def test_login_with_wrong_password(svc_ctx, tenant):
    register_user('bob2', 'pass123456', ROLE_MEMBER, tenant.id)
    user, err = authenticate('bob2', 'wrong')
    assert user is None
    assert err == '用户名或密码错误'


def test_login_unknown_user_gives_same_message(svc_ctx, tenant):
    """🔴 用户不存在与密码错必须返回**完全相同**的文案。"""

    register_user('bob3', 'pass123456', ROLE_MEMBER, tenant.id)
    _, wrong_pw = authenticate('bob3', 'wrong')
    _, no_user = authenticate('nobody', 'wrong')
    assert wrong_pw == no_user == '用户名或密码错误'


# ============ 四档角色 ============
def test_all_four_roles_exist():
    assert set(ALL_ROLES) == {'platform_admin', 'org_admin', 'member', 'viewer'}


def test_all_roles_have_labels():
    for role in ALL_ROLES:
        assert role in ROLE_LABELS


def test_platform_admin_is_cross_tenant():
    """平台管理员才有 cross_tenant 能力。"""

    assert 'cross_tenant' in PERMISSIONS[ROLE_PLATFORM_ADMIN]
    for role in (ROLE_ORG_ADMIN, ROLE_MEMBER, ROLE_VIEWER):
        assert 'cross_tenant' not in PERMISSIONS[role]


def test_org_admin_has_manage_kb():
    assert has_permission(_u(ROLE_ORG_ADMIN), 'manage_kb') is True
    assert has_permission(_u(ROLE_MEMBER), 'manage_kb') is False


def test_member_can_ask_only():
    member = _u(ROLE_MEMBER)
    assert has_permission(member, 'ask') is True
    assert has_permission(member, 'view_history') is True
    assert has_permission(member, 'manage_kb') is False
    assert has_permission(member, 'upload_doc') is False


def test_viewer_can_only_view_history():
    """🔴 只读只能看历史，不能问。"""

    viewer = _u(ROLE_VIEWER)
    assert has_permission(viewer, 'view_history') is True
    assert has_permission(viewer, 'ask') is False


def test_has_permission_rejects_none():
    assert has_permission(None, 'ask') is False


def _u(role):
    return type('U', (), {'role': role, 'username': 'x'})()


# ============ 权限矩阵完整性 ============
def test_permission_matrix_is_disjoint_for_viewer():
    """viewer 的能力集合必须是最小的。"""

    viewer = set(PERMISSIONS[ROLE_VIEWER])
    assert viewer == {'view_history'}, f'viewer 能力应只有 view_history，实际 {viewer}'


def test_member_is_superset_of_viewer():
    assert set(PERMISSIONS[ROLE_VIEWER]) < set(PERMISSIONS[ROLE_MEMBER])


def test_org_admin_is_superset_of_member():
    assert set(PERMISSIONS[ROLE_MEMBER]) < set(PERMISSIONS[ROLE_ORG_ADMIN])