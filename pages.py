"""页面路由（阶段 6）。

## 为什么单独一个文件

`app.py` 负责应用工厂与核心 API，页面是**展示层**，混进去会让 app.py 越来越难读。

## 🔴 权限门控放在服务端，不是只靠前端隐藏

前端按角色隐藏菜单只是**体验**；真正的门控必须在这里（和 API 层一致）。
比如成员直接手输 `/platform`，服务端也要挡住。
"""
import logging

from flask import redirect, render_template, session, url_for

from auth import current_user, has_permission


logger = logging.getLogger('rmb01.pages')


def _redirect_if_anonymous():
    """未登录 → 跳登录页。返回 None 表示已登录。"""

    me = current_user()
    if me is None:
        return redirect(url_for('page_login'))
    return None


def register_page_routes(app):
    """挂 4 个页面路由。"""

    @app.get('/login')
    def page_login():
        """登录 / 注册页。已登录的话由前端跳走。"""

        if current_user() is not None:
            return redirect(url_for('page_kbs'))
        return render_template('login.html')

    @app.get('/kbs')
    def page_kbs():
        """知识库主页 —— 演示的核心：列表 + 上传 + 问答带引用。"""

        if (resp := _redirect_if_anonymous()) is not None:
            return resp
        return render_template('kbs.html', active='/kbs')

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
            logger.info('page_denied', extra={
                'event': 'page_denied', 'path': '/org', 'role': user.role,
            })
            return render_template('denied.html',
                                   message='企业后台仅企业管理员可访问'), 403

        return render_template('org.html', active='/org')

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
            logger.info('page_denied', extra={
                'event': 'page_denied', 'path': '/platform', 'role': user.role,
            })
            return render_template('denied.html',
                                   message='只有平台管理员可以访问'), 403

        return render_template('platform.html', active='/platform')

    return app