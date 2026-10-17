"""运营后台 + 企业后台。

🔴 **租户隔离在这里最容易出事** —— 后台是「看别人数据」的场景，
一个 filter 漏了就是数据泄漏。所以：

  · 企业后台的每个接口都经scoped()，跨租户一律 404
  · 运营后台（platform_admin）是**唯一**能跨租户的��色，
    且它的跨租户能力必须由权限矩阵显式授予（cross_tenant）
  · 两类后台的接口不共用：企业管理员调运营接口 → 403
"""
import logging
import re

from flask import jsonify, request

from auth import current_user, fetch_scoped, has_permission, login_required
from billing import (
    PLANS,
    PlanDowngradeConflict,
    change_plan,
    ensure_subscription,
    quota_status,
    usage_summary,
)
from models import (
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    Kb,
    Subscription,
    Tenant,
    User,
    db,
)
from tenancy import scoped


logger = logging.getLogger('rmb01.admin')


def _tenant_id():
    """当前用户的租户 id。平台管理员没有租户 —— 返回 None。"""

    user = current_user()
    if user is None or user.role == ROLE_PLATFORM_ADMIN:
        return None
    return user.tenant_id


# ============ 企业后台 ============
def org_billing():
    """本租户的账单与用量。跨租户访问不到（因为 scoped 按 tenant_id 过滤）。"""

    tenant_id = _tenant_id()
    if tenant_id is None:
        return None

    subscription = ensure_subscription(tenant_id)
    return {
        'tenant_id': tenant_id,
        'quota': quota_status(tenant_id),
        'usage': usage_summary(tenant_id),
        'plan_name': PLANS.get(subscription.plan, {}).get('name', subscription.plan),
        'kb_count': scoped(Kb, tenant_id).count(),
        'member_count': scoped(User, tenant_id).count(),
    }


def org_members():
    """本租户成员列表。"""

    tenant_id = _tenant_id()
    if tenant_id is None:
        return None

    rows = scoped(User, tenant_id).order_by(User.id).all()
    return [
        {
            'id': u.id,
            'username': u.username,
            'role': u.role,
            'created_at': u.created_at.strftime('%Y-%m-%d') if u.created_at else None,
        }
        for u in rows
    ]


def org_change_member_role(user_id, new_role):
    """调整成员角色。返回 (result, error)。

    🔴 跨租户取不到 → 返回 (None, None) —— 与「参数不对」的 (None, error)
       区分开，上层据此返 404 而不是 400。
    """

    tenant_id = _tenant_id()
    if tenant_id is None:
        return None, None

    user = fetch_scoped(User, user_id)      # 🔴 跨租户 → None → 404
    if user is None:
        return None, None

    # 不允许把自己降级（否则可能把租户最后一个管理员弄没）
    if user.id == current_user().id and new_role != ROLE_ORG_ADMIN:
        return None, '不能修改自己的角色'

    if new_role not in (ROLE_ORG_ADMIN, 'member', ROLE_VIEWER):
        return None, '角色无效'

    user.role = new_role
    db.session.commit()
    return {'id': user.id, 'role': user.role}, None


# ============ 运营后台 ============
def platform_tenants():
    """全部租户（仅平台管理员）。"""

    rows = Tenant.query.order_by(Tenant.id).all()
    out = []
    for tenant in rows:
        sub = Subscription.query.filter_by(tenant_id=tenant.id).first()
        out.append({
            'id': tenant.id,
            'name': tenant.name,
            'slug': tenant.slug,
            'plan': sub.plan if sub else None,
            'quota_used': sub.quota_used if sub else 0,
            'quota_total': sub.quota_total if sub else 0,
            'member_count': User.query.filter_by(tenant_id=tenant.id).count(),
            'kb_count': Kb.query.filter_by(tenant_id=tenant.id).count(),
        })
    return out


def platform_usage_ranking(limit=10):
    """按已用额度排序的租户排行。"""

    rows = (
        db.session.query(Subscription)
        .order_by(Subscription.quota_used.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    names = {
        t.id: t.name for t in Tenant.query.all()
    }
    return [
        {
            'rank': i + 1,
            'tenant_id': sub.tenant_id,
            'tenant_name': names.get(sub.tenant_id, f'#{sub.tenant_id}'),
            'plan': sub.plan,
            'quota_used': sub.quota_used,
            'quota_total': sub.quota_total,
        }
        for i, sub in enumerate(rows)
    ]


def platform_change_plan(tenant_id, new_plan):
    """平台管理员变更租户套餐。返回 (result, error)。"""

    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        return None, '租户不存在'

    try:
        subscription = change_plan(tenant_id, new_plan)
    except PlanDowngradeConflict as exc:
        return None, str(exc)
    except ValueError as exc:
        return None, str(exc)

    return {
        'tenant_id': tenant_id,
        'plan': subscription.plan,
        'quota_total': subscription.quota_total,
        'quota_used': subscription.quota_used,
    }, None


# ============ 路由 ============
def register_admin_routes(app):
    """挂后台路由。"""

    # --- 企业后台 ---
    @app.get('/api/org/billing')
    @login_required('view_history')
    def api_org_billing():
        data = org_billing()
        if data is None:
            return jsonify({'error': '当前账号不属于任何租户'}), 403
        return jsonify(data)

    @app.get('/api/org/members')
    @login_required('manage_org_users')
    def api_org_members():
        rows = org_members()
        if rows is None:
            return jsonify({'error': '当前账号不属于任何租户'}), 403
        return jsonify(rows)

    @app.put('/api/org/members/<int:user_id>/role')
    @login_required('manage_org_users')
    def api_org_change_role(user_id):
        payload = request.get_json(silent=True) or {}
        result, error = org_change_member_role(user_id, payload.get('role'))
        if error:
            return jsonify({'error': error}), 400
        if result is None:
            # 🔴 跨租户 / 不存在 → 404（不返 403，403 会泄漏 id 存在）
            return jsonify({'error': '用户不存在'}), 404
        return jsonify(result)

    # --- 运营后台 ---
    @app.get('/api/platform/tenants')
    @login_required('cross_tenant')
    def api_platform_tenants():
        return jsonify(platform_tenants())

    @app.get('/api/platform/usage-ranking')
    @login_required('cross_tenant')
    def api_platform_usage_ranking():
        limit = request.args.get('limit', default=10, type=int)
        return jsonify(platform_usage_ranking(limit))

    @app.put('/api/platform/tenants/<int:tenant_id>/plan')
    @login_required('cross_tenant')
    def api_platform_change_plan(tenant_id):
        payload = request.get_json(silent=True) or {}
        result, error = platform_change_plan(tenant_id, payload.get('plan'))
        if error:
            # 降级冲突是业务冲突 → 409，不是 400
            code = 409 if '超过' in error else 400
            return jsonify({'error': error}), code
        return jsonify(result)

    @app.get('/api/platform/plans')
    @login_required('cross_tenant')
    def api_platform_plans():
        return jsonify([
            {
                'plan': key,
                'name': spec['name'],
                'quota_total': spec['quota_total'],
                'price_cny_month': spec['price_cny_month'],
                'features': spec['features'],
            }
            for key, spec in PLANS.items()
        ])

    # --- 注册用：创建租户 ---
    @app.post('/api/tenants')
    def api_create_tenant():
        """注册流程第一步：建租户。

        🔴 这个接口**故意不需要登录** —— 新用户还没有账号，
        必然是在未登录状态下建租户。

        风险与对策：
          · 任何未登录的人都能建租户 → 会被刷垃圾租户
            ⇒ 限流 / 邀请码在真实环境是必需的（本项目未实现，已在
              README「未验证」里标注）
          · 租户名重复 → 返回 409，不自动改名（改名会让用户困惑）
        """

        payload = request.get_json(silent=True) or {}
        name = (payload.get('name') or '').strip()
        if not name:
            return jsonify({'error': '租户名不能为空'}), 400
        if len(name) > 60:
            return jsonify({'error': '租户名过长'}), 400

        if Tenant.query.filter_by(name=name).first() is not None:
            return jsonify({'error': '该名称已被使用，请换一个'}), 409

        # slug 由名称派生；碰撞时追加序号
        base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'tenant'
        slug = base
        n = 2
        while Tenant.query.filter_by(slug=slug).first() is not None:
            slug = f'{base}-{n}'
            n += 1

        tenant = Tenant(name=name, slug=slug)
        db.session.add(tenant)
        db.session.commit()
        ensure_subscription(tenant.id)      # 默认免费版

        logger.info('tenant_created', extra={
            'event': 'tenant_created', 'tenant_id': tenant.id, 'name': name,
        })
        return jsonify({'id': tenant.id, 'name': tenant.name, 'slug': tenant.slug}), 201

    return app