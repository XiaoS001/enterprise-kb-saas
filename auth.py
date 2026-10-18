"""认证与授权。

安全底线（照RMB03 的 auth.py，跨项目保持一致）：
  · 密码**只存 hash**：PBKDF2-SHA256 · 12 万轮 · **每用户随机盐**
  · `hmac.compare_digest` 常量时间比较，防计时侧信道
  · **不区分「用户不存在」与「密码错」**，统一返「用户名或密码错误」→ 防用户名枚举
  · 登录失败一律 401，不是 500
"""
import functools
import hashlib
import hmac
import logging
import os

from flask import jsonify, session

import config
from models import (
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ALL_ROLES,
    User,
    db,
)
from tenancy import scoped


logger = logging.getLogger('rmb01.auth')

_PBKDF2_ROUNDS = 120_000
_HASH_PREFIX = 'pbkdf2_sha256'


def hash_password(password, salt=None):
    """生成密码 hash：pbkdf2_sha256$rounds$salt_hex$hash_hex。

    salt 缺省取环境变量 RMB01_PASSWORD_SALT；没配时用随机盐
    （每用户不同 → 彩虹表无效；且不会所有部署共用一个盐）。
    """

    if salt is None:
        salt = config.PASSWORD_SALT
    if not salt:
        salt = os.urandom(16).hex()

    salt_bytes = salt.encode('utf-8')
    digest = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), salt_bytes, _PBKDF2_ROUNDS,
    )
    return f'{_HASH_PREFIX}${_PBKDF2_ROUNDS}${salt_bytes.hex()}${digest.hex()}'


def verify_password(password, stored_hash):
    """常量时间比较。任何格式异常都返 False，不抛。"""

    try:
        algorithm, rounds, salt_hex, digest_hex = stored_hash.split('$')
        if algorithm != _HASH_PREFIX:
            return False
        expected = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            bytes.fromhex(salt_hex),
            int(rounds),
        ).hex()
        return hmac.compare_digest(expected, digest_hex)
    except (ValueError, AttributeError, TypeError):
        return False


def register_user(username, password, role=ROLE_ORG_ADMIN, tenant_id=None):
    """注册用户。返回 (user, None) 或 (None, error)。

    ⚠️ 仅供内部/测试使用。🔴 **HTTP 入口不再用这个函数** ——
    它接受调用方传入 role/tenant_id，会把「身份授予」交给请求方。
    对外注册必须走 register_new_tenant（强制 org_admin + 新建租户）。
    """

    if not username or not password:
        return None, '用户名与密码不能为空'
    if len(username) < 3:
        return None, '用户名至少 3 个字符'
    if len(password) < 6:
        return None, '密码至少 6 个字符'
    if role not in ALL_ROLES:
        return None, f'角色必须是 {"/".join(ALL_ROLES)} 之一'

    # 🔴 用户名全局唯一（跨租户），所以这里不用 scoped ——
    #    否则租户 A 能注册出与租户 B 相同的用户名，造成歧义。
    if User.query.filter_by(username=username).first() is not None:
        return None, '用户名已存在'

    if role == ROLE_PLATFORM_ADMIN:
        tenant_id = None          # 平台管理员不属于任何租户
    elif tenant_id is None:
        return None, '非平台管理员必须指定 tenant_id'

    user = User(
        tenant_id=tenant_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    return user, None


def register_new_tenant(username, password, tenant_name=None):
    """🔴 对外注册唯一入口：新建租户 + 该租户第一个 org_admin。

    🔴 安全设计（修复 WorkBuddy 审查发现的提权漏洞）：
    - **强制 role = org_admin**：请求体里的 role 一律忽略 —— 任何人都不能
      通过注册把自己变成 platform_admin（那会拿到 cross_tenant 权限）
    - **强制新建租户**：请求体里的 tenant_id 一律忽略 —— 任何人都不能
      通过注册加入一个已有租户（那会看到别人的知识库）
    - platform_admin 唯一来源：`init_platform_admin`（CLI / seed 脚本）

    返回 (user, None) 或 (None, error)。
    """

    from admin_api import _create_tenant_slug  # 复用 slug 派生（幂等）

    if not username or not password:
        return None, '用户名与密码不能为空'
    if len(username) < 3:
        return None, '用户名至少 3 个字符'
    if len(password) < 6:
        return None, '密码至少 6 个字符'
    if User.query.filter_by(username=username).first() is not None:
        return None, '用户名已存在'

    tenant_name = (tenant_name or username).strip()
    if not tenant_name:
        return None, '租户名不能为空'

    from models import Tenant

    tenant = Tenant(name=tenant_name, slug=_create_tenant_slug(tenant_name))
    db.session.add(tenant)
    db.session.flush()          # 拿到 tenant.id，不落库先取主键

    from billing import ensure_subscription

    ensure_subscription(tenant.id)      # 默认免费版

    user = User(
        tenant_id=tenant.id,
        username=username,
        password_hash=hash_password(password),
        role=ROLE_ORG_ADMIN,            # 🔴 强制 org_admin
    )
    db.session.add(user)
    db.session.commit()
    return user, None


def authenticate(username, password):
    """校验凭据。返回 (user, None) 或 (None, error)。"""

    user = User.query.filter_by(username=username).first()
    if user is None:
        # 不区分「用户不存在」与「密码错」
        return None, '用户名或密码错误'
    if not verify_password(password, user.password_hash):
        return None, '用户名或密码错误'
    return user, None


# ============ 权限矩阵 ============
# 能力名而不是路由路径：改路由时不会漏改鉴权。
PERMISSIONS = {
    ROLE_PLATFORM_ADMIN: {
        'cross_tenant', 'manage_tenants', 'manage_billing',
        'view_all_usage', 'manage_kb', 'upload_doc', 'ask', 'view_history',
    },
    ROLE_ORG_ADMIN: {
        'manage_kb', 'upload_doc', 'ask', 'view_history', 'manage_org_users',
    },
    'member': {'ask', 'view_history'},
    'viewer': {'view_history'},
}


def has_permission(user, permission):
    if user is None:
        return False
    return permission in PERMISSIONS.get(user.role, set())


def login_required(permission=None, need_tenant=True):
    """未登录 401 · 无权限 403 · 无租户上下文 403。"""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            user_id = session.get('user_id')
            if user_id is None:
                return jsonify({'error': '请先登录'}), 401

            user = db.session.get(User, user_id)
            if user is None:
                session.clear()
                return jsonify({'error': '请先登录'}), 401

            if permission is not None and not has_permission(user, permission):
                logger.warning('permission_denied', extra={
                    'event': 'permission_denied',
                    'username': user.username,
                    'role': user.role,
                    'permission': permission,
                })
                return jsonify({
                    'error': '无权访问',
                    'required_permission': permission,
                }), 403

            if need_tenant and user.tenant_id is None \
                    and user.role != ROLE_PLATFORM_ADMIN:
                return jsonify({'error': '当前账号未绑定租户'}), 403

            return view(*args, **kwargs)

        return wrapper

    return decorator


def current_user():
    user_id = session.get('user_id')
    if user_id is None:
        return None
    return db.session.get(User, user_id)


def current_tenant_id():
    """🔴 当前租户 id —— **只从会话取**，绝不从请求参数取。"""

    user = current_user()
    if user is None or user.role == ROLE_PLATFORM_ADMIN:
        return None
    return user.tenant_id


def fetch_scoped(model, record_id):
    """🔴 按 id 取一条租户级记录，**取不到就当作不存在**。

    跨租户统一返 None（上层转 404），不返 403——
    403 会泄漏「这个资源存在」。

    平台管理员走平台管理员口径（它的职责就是跨租户）。
    """

    user = current_user()
    if user is not None and user.role == ROLE_PLATFORM_ADMIN:
        return model.query.filter_by(id=record_id).first()
    return scoped(model, current_tenant_id()).filter_by(id=record_id).first()


# ============ 认证路由 ============
def register_auth_routes(application):
    """注册 / 注册 / 登出 / 当前用户。"""

    import config
    from flask import jsonify, request, session
    from models import ALL_ROLES, ROLE_PLATFORM_ADMIN, User, db
    from tenancy import set_tenant_context

    @application.post('/api/auth/register')
    def api_register():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': '缺少参数'}), 400

        # 🔴 修复提权漏洞：只新建租户 + 该租户第一个 org_admin。
        #    请求体里的 role / tenant_id **一律忽略** —— 任何人都不能
        #    通过注册成为 platform_admin 或加入已有租户。
        user, error = register_new_tenant(
            username=payload.get('username'),
            password=payload.get('password'),
            tenant_name=payload.get('tenant_name'),
        )
        if error:
            return jsonify({'error': error}), 400
        return jsonify({
            'id': user.id,
            'username': user.username,
            'role': user.role,
            'tenant_id': user.tenant_id,
        }), 201

    @application.post('/api/auth/login')
    def api_login():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': '缺少参数'}), 400

        user, error = authenticate(
            payload.get('username', ''), payload.get('password', ''),
        )
        if error:
            return jsonify({'error': error}), 401

        session['user_id'] = user.id
        logger.info('user_login', extra={
            'event': 'user_login',
            'username': user.username,
            'role': user.role,
        })
        return jsonify({
            'username': user.username,
            'role': user.role,
            'tenant_id': user.tenant_id,
        })

    @application.post('/api/auth/logout')
    def api_logout():
        session.clear()
        return jsonify({'status': 'ok'})

    @application.get('/api/auth/me')
    @login_required()
    def api_me():
        user = current_user()
        return jsonify({
            'username': user.username,
            'role': user.role,
            'tenant_id': user.tenant_id,
            'permissions': sorted(PERMISSIONS.get(user.role, set())),
        })

    return application