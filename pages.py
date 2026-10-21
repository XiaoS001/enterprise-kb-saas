"""页面路由（阶段 6 / 第一次维护后扩到 10 个页面）。

## 为什么单独一个文件

`app.py` 负责应用工厂与核心 API，页面是**展示层**，混进去会让 app.py 越来越难读。

## 🔴 权限门控放在服务端，不是只靠前端隐藏

前端按角色隐藏菜单只是**体验**；真正的门控必须在这里（和 API 层一致）。
比如成员直接手输 `/platform`，服务端也要挡住。

## 页面门控比接口更严，是有意的

| 页面 | 服务端要求 | 接口要求 | 为什么更严 |
|---|---|---|---|
| `/org` `/org/usage` | `manage_org_users` | 账单 `view_history` / 成员 `manage_org_users` | 成员看得到账单，但后台里有「改角色 / 删成员」，给他看一张改不了的表只会困惑 |
| `/compare` | `view_history`（且必须有租户） | 同 | 对比页统计的是"本库检索"，平台管理员没有库 |
| `/me` | 仅登录 | 同 | 只动自己的密码 |
"""
import logging

from flask import redirect, render_template, url_for

from auth import PERMISSIONS, current_user, has_permission
from models import (
    ROLE_LABELS,
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    Kb,
)


logger = logging.getLogger('rmb01.pages')


# 能力名 → 人话。登录页的演示账号卡片按**真实权限矩阵**渲染，
# 所以这里少写一个，登录页就会少显示一条 —— 不会出现"文档说有、实际没有"。
PERMISSION_LABELS = {
    'manage_kb': '建库删库',
    'upload_doc': '上传文档',
    'ask': '发起问答',
    'view_history': '查看历史',
    'manage_org_users': '成员管理',
    'cross_tenant': '跨租户查看',
    'manage_tenants': '租户管理',
    'manage_billing': '套餐计费',
    'view_all_usage': '全局用量',
}


# 演示账号（与 demo_seed.py 建的账号一一对应）。
# 🔴 密码统一 demo123456，只在演示环境有意义；真实部署请删掉这张表。
DEMO_PASSWORD = 'demo123456'

DEMO_ACCOUNTS = (
    ('demo-admin', ROLE_ORG_ADMIN, '演示公司 · 企业管理员',
     '能建库、上传文档、调成员角色'),
    ('demo-member', ROLE_MEMBER, '演示公司 · 成员',
     '能问答，但看不到企业后台'),
    ('demo-viewer', ROLE_VIEWER, '演示公司 · 只读',
     '只能看：提问和上传都会被拦'),
    ('demo-platform', ROLE_PLATFORM_ADMIN, '跨租户 · 平台管理员',
     '能跨租户看所有租户、改套餐'),
)


def demo_accounts():
    """按**真实权限矩阵**生成登录页的 4 张卡片。

    能力按 PERMISSION_LABELS 的书写顺序排（建库 → 上传 → 问答 → 历史 …），
    而不是字母序 —— 逐张对照时顺序一致才看得出"多了哪几项"。
    """

    order = {name: i for i, name in enumerate(PERMISSION_LABELS)}
    out = []
    for username, role, scope, try_hint in DEMO_ACCOUNTS:
        permissions = sorted(
            PERMISSIONS.get(role, set()),
            key=lambda name: (order.get(name, 99), name),
        )
        out.append({
            'username': username,
            'password': DEMO_PASSWORD,
            'role': role,
            'role_label': ROLE_LABELS.get(role, role),
            'scope': scope,
            'try_hint': try_hint,
            'permissions': permissions,
            'permission_labels': [
                PERMISSION_LABELS.get(name, name) for name in permissions
            ],
        })
    return out


def _redirect_if_anonymous():
    """未登录 → 跳登录页。返回 None 表示已登录。"""

    if current_user() is None:
        return redirect(url_for('page_login'))
    return None


def _deny(message, path):
    user = current_user()
    logger.info('page_denied', extra={
        'event': 'page_denied',
        'path': path,
        'role': user.role if user else None,
    })
    return render_template('denied.html', message=message), 403


def _gate(permission, message, path):
    """登录 + 能力双重门控。返回 None 表示放行。"""

    if (resp := _redirect_if_anonymous()) is not None:
        return resp
    if not has_permission(current_user(), permission):
        return _deny(message, path)
    return None


def _gate_kb(kb_id, path):
    """知识库页面的门控：登录 + 库属于本租户。

    🔴 跨租户访问返**同一个 404 页面**（不是 403）——
    否则「这个 id 存在但你没权限」本身就是泄漏。
    """

    if (resp := _redirect_if_anonymous()) is not None:
        return resp

    user = current_user()
    if user.role == ROLE_PLATFORM_ADMIN:
        # 平台管理员没有自己的租户，也就没有"自己的库"
        return _deny('平台管理员没有自己的知识库，请到「平台后台」按租户查看',
                     path)

    if not has_permission(user, 'view_history'):
        return _deny('当前角色没有查看知识库的权限', path)

    kb = Kb.query.filter_by(id=kb_id, tenant_id=user.tenant_id).first()
    if kb is None:
        return render_template('denied.html', message='知识库不存在'), 404
    return None


def register_page_routes(app):
    """挂 10 个页面路由。"""

    @app.get('/login')
    def page_login():
        """登录 / 注册页。已登录的话直接跳知识库。"""

        if (user := current_user()) is not None:
            # 平台管理员没有租户，跳 /kbs 只会再被弹回来
            if user.tenant_id is None:
                return redirect(url_for('page_platform'))
            return redirect(url_for('page_kbs'))
        return render_template(
            'login.html',
            demo_accounts=demo_accounts(),
            demo_password=DEMO_PASSWORD,
        )

    @app.get('/kbs')
    def page_kbs():
        """知识库列表 —— 演示的入口。"""

        if (resp := _redirect_if_anonymous()) is not None:
            return resp
        if current_user().tenant_id is None:
            # 平台账号没有自己的库 —— 与其让页面里的接口全报错，不如去平台后台
            return redirect(url_for('page_platform'))
        return render_template('kbs.html', active='/kbs')

    @app.get('/kbs/<int:kb_id>')
    def page_kb_detail(kb_id):
        """单个知识库：文档列表 + 上传 + 分段预览。"""

        if (resp := _gate_kb(kb_id, f'/kbs/{kb_id}')) is not None:
            return resp
        return render_template('kb_detail.html', kb_id=kb_id, active='/kbs')

    @app.get('/kbs/<int:kb_id>/ask')
    def page_kb_ask(kb_id):
        """问答页：答案 + 引用 + 降级说明。

        `viewer` 能看库但不能提问 —— 这里挡的是能力，不是身份。
        """

        if (resp := _gate_kb(kb_id, f'/kbs/{kb_id}/ask')) is not None:
            return resp

        user = current_user()
        if not has_permission(user, 'ask'):
            return _deny('只读账号不能发起问答', f'/kbs/{kb_id}/ask')

        return render_template('kb_ask.html', kb_id=kb_id, active='/kbs')

    @app.get('/compare')
    def page_compare():
        """检索策略对比：同一份库、两种口径现场跑。"""

        if (resp := _gate('view_history', '当前角色没有查看检索对比的权限',
                          '/compare')) is not None:
            return resp

        user = current_user()
        if user.role == ROLE_PLATFORM_ADMIN:
            return _deny('检索对比统计的是本租户的知识库，平台管理员没有库',
                         '/compare')

        return render_template('compare.html', active='/compare')

    @app.get('/org')
    def page_org():
        """企业后台：账单 + 成员管理。

        🔴 权限门控：只给 `manage_org_users`（企业管理员）。

        为什么不用 `view_history`：那个权限成员和只读也有
        （他们该能看到自己用了多少次），但**「企业后台」包含成员管理**，
        让成员进去看到一张改不了的表只会困惑。
        所以页面的门控比 API 更严：API 允许成员读账单，页面只给管理员。
        """

        if (resp := _redirect_if_anonymous()) is not None:
            return resp

        user = current_user()
        if not has_permission(user, 'manage_org_users'):
            return _deny('企业后台仅企业管理员可访问', '/org')
        return render_template('org.html', active='/org')

    @app.get('/org/usage')
    def page_org_usage():
        """用量明细：一次一行，带降级标记与提问人。"""

        if (resp := _gate('manage_org_users', '用量明细仅企业管理员可访问',
                          '/org/usage')) is not None:
            return resp
        return render_template('org_usage.html', active='/org/usage')

    @app.get('/platform')
    def page_platform():
        """平台后台：跨租户视角。

        🔴 只有 platform_admin 有 `cross_tenant` 权限能进。
        企业管理员手输 URL 也会被这里挡住（403 页面）。
        """

        if (resp := _redirect_if_anonymous()) is not None:
            return resp

        user = current_user()
        if not has_permission(user, 'cross_tenant'):
            return _deny('只有平台管理员可以访问', '/platform')
        return render_template('platform.html', active='/platform')

    @app.get('/platform/tenants/<int:tenant_id>')
    def page_tenant_detail(tenant_id):
        """单个租户画像（平台管理员）。"""

        if (resp := _gate('cross_tenant', '只有平台管理员可以访问',
                          f'/platform/tenants/{tenant_id}')) is not None:
            return resp
        return render_template('tenant_detail.html', tenant_id=tenant_id,
                               active='/platform')

    @app.get('/me')
    def page_me():
        """个人中心：账号信息 + 改密码。"""

        if (resp := _redirect_if_anonymous()) is not None:
            return resp
        return render_template('me.html', active='/me')

    return app
