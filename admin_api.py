"""运营后台 + 企业后台。

🔴 **租户隔离在这里最容易出事** —— 后台是「看别人数据」的场景，
一个 filter 漏了就是数据泄漏。所以：

  · 企业后台的每个接口都经scoped()，跨租户一律 404
  · 运营后台（platform_admin）是**唯一**能跨租户的��色，
    且它的跨租户能力必须由权限矩阵显式授予（cross_tenant）
  · 两类后台的接口不共用：企业管理员调运营接口 → 403
"""
import datetime
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
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    Document,
    Kb,
    Subscription,
    Tenant,
    Usage,
    User,
    db,
)
from tenancy import scoped


logger = logging.getLogger('rmb01.admin')


def _create_tenant_slug(name):
    """由租户名派生唯一 slug（碰撞时追加序号）。幂等、可复用。"""
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'tenant'
    slug = base
    n = 2
    while Tenant.query.filter_by(slug=slug).first() is not None:
        slug = f'{base}-{n}'
        n += 1
    return slug


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


def org_add_member(username, password, role):
    """企业管理员给**自己的租户**加成员。返回 (user, None) 或 (None, error)。

    🔴 安全约束（补 WorkBuddy 复验发现的"加成员"缺口）：
    - 只能加到**当前租户**（tenant_id 取自会话，不是请求体）
    - 只能加 member / viewer —— 请求体传 platform_admin / org_admin 一律拒绝
    - 请求体里的 tenant_id / role 以外的权限位一律忽略
    """

    from auth import register_user

    tenant_id = _tenant_id()
    if tenant_id is None:
        return None, '当前账号不属于任何租户'

    if role not in (ROLE_MEMBER, ROLE_VIEWER):
        return None, '只能添加成员或只读账号'

    user, error = register_user(username, password, role, tenant_id)
    if error:
        return None, error
    return user, None


def org_delete_member(user_id):
    """企业管理员移除**本租户**成员。返回 (result, error)。

    与改角色同一套口径：跨租户 / 不存在 → (None, None) → 上层 404。

    两条护栏（都是「删完才发现」的类型）：
      · 不能删自己 —— 删完自己就登不进去了，只能让平台管理员来救
      · 不能删掉最后一个企业管理员 —— 否则这个租户再没人能管成员，
        而平台管理员并没有「进租户内改角色」的入口
    """

    tenant_id = _tenant_id()
    if tenant_id is None:
        return None, None

    target = fetch_scoped(User, user_id)      # 🔴 跨租户 → None → 404
    if target is None or target.tenant_id != tenant_id:
        return None, None

    me = current_user()
    if me is not None and target.id == me.id:
        return None, '不能删除自己的账号'

    if target.role == ROLE_ORG_ADMIN:
        admins = scoped(User, tenant_id).filter_by(role=ROLE_ORG_ADMIN).count()
        if admins <= 1:
            return None, '租户至少要保留一个企业管理员'

    username = target.username
    db.session.delete(target)
    db.session.commit()

    logger.info('member_removed', extra={
        'event': 'member_removed',
        'tenant_id': tenant_id,
        'removed_username': username,
        'operator': me.username if me else None,
    })
    return {'id': user_id, 'username': username, 'deleted': True}, None


def org_usage_detail(page=1, page_size=20):
    """本租户问答流水（分页）。只返回**自己租户**的行。

    🔴 分页参数一律先钳位再进 SQL：`page_size=10**9` 直接 offset 也能跑，
    但那等于把整表捞出来 —— 是最好被利用的 DoS 入口之一。
    """

    tenant_id = _tenant_id()
    if tenant_id is None:
        return None

    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = 20
    page = max(1, page)
    page_size = max(1, min(page_size, 100))

    query = scoped(Usage, tenant_id)
    total = query.count()
    rows = (
        query.order_by(Usage.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    pages = (total + page_size - 1) // page_size if total else 0

    names = {
        u.id: u.username for u in scoped(User, tenant_id).all()
    }

    return {
        'items': [
            {
                'id': row.id,
                'question': row.question,
                'kb_id': row.kb_id,
                'username': names.get(row.user_id),
                'tokens': row.tokens,
                'degraded': bool(row.degraded),
                'created_at': row.created_at.strftime('%Y-%m-%d %H:%M:%S')
                if row.created_at else None,
            }
            for row in rows
        ],
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': pages,
        'summary': usage_summary(tenant_id),
        'quota': quota_status(tenant_id),
    }


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


def platform_tenant_detail(tenant_id, days=7):
    """单个租户的完整画像（仅平台管理员）。取不到返回 None。

    🔴 这里**故意不用 scoped()** —— 平台管理员的职责就是跨租户，
    它的越权由权限矩阵的 cross_tenant 显式授予（而不是靠"忘了过滤"）。
    与之相对，企业后台那几个接口必须 scoped，两者不能混用一个函数。
    """

    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        return None

    sub = Subscription.query.filter_by(tenant_id=tenant_id).first()
    members = User.query.filter_by(tenant_id=tenant_id).order_by(User.id).all()
    kbs = Kb.query.filter_by(tenant_id=tenant_id).order_by(Kb.id).all()

    doc_counts = dict(
        db.session.query(Document.kb_id, db.func.count(Document.id))
        .filter(Document.tenant_id == tenant_id)
        .group_by(Document.kb_id)
        .all()
    )

    days = max(1, min(int(days or 7), 30))
    today = datetime.date.today()
    start_day = today - datetime.timedelta(days=days - 1)
    start_dt = datetime.datetime.combine(start_day, datetime.time.min)

    recent_rows = (
        Usage.query.filter(
            Usage.tenant_id == tenant_id,
            Usage.created_at >= start_dt,
        )
        .order_by(Usage.id)
        .all()
    )

    buckets = {
        (start_day + datetime.timedelta(days=i)).strftime('%m-%d'):
            {'total': 0, 'degraded': 0}
        for i in range(days)
    }
    for row in recent_rows:
        if row.created_at is None:
            continue
        key = row.created_at.strftime('%m-%d')
        if key not in buckets:
            continue
        buckets[key]['total'] += 1
        if row.degraded:
            buckets[key]['degraded'] += 1

    last_rows = (
        Usage.query.filter_by(tenant_id=tenant_id)
        .order_by(Usage.id.desc())
        .limit(8)
        .all()
    )
    names = {u.id: u.username for u in members}

    quota_used = sub.quota_used if sub else 0
    quota_total = sub.quota_total if sub else 0

    return {
        'id': tenant.id,
        'name': tenant.name,
        'slug': tenant.slug,
        'created_at': tenant.created_at.strftime('%Y-%m-%d') if tenant.created_at else None,
        'plan': sub.plan if sub else None,
        'plan_name': PLANS.get(sub.plan, {}).get('name', sub.plan) if sub else '未订阅',
        'quota_used': quota_used,
        'quota_total': quota_total,
        'quota_ratio': round(quota_used / quota_total * 100, 1) if quota_total else 0.0,
        'usage': usage_summary(tenant_id),
        'counts': {
            'members': len(members),
            'kbs': len(kbs),
            'documents': sum(doc_counts.values()),
        },
        'members': [
            {
                'id': u.id,
                'username': u.username,
                'role': u.role,
                'created_at': u.created_at.strftime('%Y-%m-%d') if u.created_at else None,
            }
            for u in members
        ],
        'kbs': [
            {
                'id': kb.id,
                'name': kb.name,
                'document_count': int(doc_counts.get(kb.id, 0)),
            }
            for kb in kbs
        ],
        'daily': [
            {'date': key, **value} for key, value in buckets.items()
        ],
        'recent': [
            {
                'id': row.id,
                'question': row.question,
                'username': names.get(row.user_id),
                'degraded': bool(row.degraded),
                'created_at': row.created_at.strftime('%m-%d %H:%M')
                if row.created_at else None,
            }
            for row in last_rows
        ],
    }


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

    @app.post('/api/org/members')
    @login_required('manage_org_users')
    def api_org_add_member():
        payload = request.get_json(silent=True) or {}
        # 🔴 只允许 member/viewer；role 传 platform_admin / org_admin → 400
        user, error = org_add_member(
            payload.get('username'),
            payload.get('password'),
            payload.get('role', ROLE_MEMBER),
        )
        if error:
            return jsonify({'error': error}), 400
        return jsonify({
            'id': user.id,
            'username': user.username,
            'role': user.role,
            'tenant_id': user.tenant_id,
        }), 201

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

    @app.delete('/api/org/members/<int:user_id>')
    @login_required('manage_org_users')
    def api_org_delete_member(user_id):
        result, error = org_delete_member(user_id)
        if error:
            return jsonify({'error': error}), 400
        if result is None:
            return jsonify({'error': '用户不存在'}), 404
        return jsonify(result)

    @app.get('/api/org/usage')
    @login_required('manage_org_users')
    def api_org_usage():
        data = org_usage_detail(
            request.args.get('page', default=1, type=int),
            request.args.get('page_size', default=20, type=int),
        )
        if data is None:
            return jsonify({'error': '当前账号不属于任何租户'}), 403
        return jsonify(data)

    # --- 运营后台 ---
    @app.get('/api/platform/tenants')
    @login_required('cross_tenant')
    def api_platform_tenants():
        return jsonify(platform_tenants())

    @app.get('/api/platform/tenants/<int:tenant_id>')
    @login_required('cross_tenant')
    def api_platform_tenant_detail(tenant_id):
        data = platform_tenant_detail(tenant_id)
        if data is None:
            return jsonify({'error': '租户不存在'}), 404
        return jsonify(data)

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

        tenant = Tenant(name=name, slug=_create_tenant_slug(name))
        db.session.add(tenant)
        db.session.commit()
        ensure_subscription(tenant.id)      # 默认免费版

        logger.info('tenant_created', extra={
            'event': 'tenant_created', 'tenant_id': tenant.id, 'name': name,
        })
        return jsonify({'id': tenant.id, 'name': tenant.name, 'slug': tenant.slug}), 201

    return app