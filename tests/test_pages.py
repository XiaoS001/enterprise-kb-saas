"""页面路由与权限门控测试（阶段 6）。

🔴 这组的重点：**页面门控必须和 API 门控在同一套规则上**，
而且服务端要真的挡住，不能只靠前端隐藏菜单。

实测过的坑：一开始 `/org` 用的是 `view_history` 权限，
但成员和只读也有那个权限 —— 结果成员能进「企业后台」看到一张改不了的成员表。
页面的门控比 API 更严：API 允许成员读账单，页面只给 `manage_org_users`。
"""
import pytest

pytest_plugins = ['tenant_fixtures']

from auth import register_user
from billing import ensure_subscription
from models import (
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    Tenant,
    db,
)


@pytest.fixture()
def pages_env(app_with_db):
    a = Tenant(name='甲公司', slug='tenant-a')
    b = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([a, b])
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
    return {'a': a, 'b': b, **accounts}


def _login(client, username):
    return client.post('/api/auth/login',
                       json={'username': username, 'password': 'pass123456'})


# ============ 未登录一律跳登录页 ============
@pytest.mark.parametrize('path', ['/kbs', '/org', '/platform'])
def test_anonymous_redirected_to_login(app_with_db, pages_env, path):
    r = app_with_db.test_client().get(path, follow_redirects=False)

    assert r.status_code == 302
    assert r.headers['Location'].endswith('/login')


def test_root_redirects_anonymous_to_login(app_with_db, pages_env):
    r = app_with_db.test_client().get('/', follow_redirects=False)

    assert r.status_code == 302
    assert r.headers['Location'].endswith('/login')


def test_root_redirects_logged_in_to_kbs(app_with_db, pages_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    r = c.get('/', follow_redirects=False)

    assert r.status_code == 302
    assert r.headers['Location'].endswith('/kbs')


def test_login_page_renders_for_anonymous(app_with_db, pages_env):
    r = app_with_db.test_client().get('/login')

    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '登录' in body
    assert '用户名' in body


def test_login_page_redirects_logged_in_user(app_with_db, pages_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    r = c.get('/login', follow_redirects=False)

    assert r.status_code == 302
    assert r.headers['Location'].endswith('/kbs')


# ============ 🔴 页面权限矩阵（服务端强制）============
def test_org_admin_can_open_kbs_and_org_but_not_platform(app_with_db, pages_env):
    c = app_with_db.test_client()
    _login(c, 'Aadmin')

    assert c.get('/kbs').status_code == 200
    assert c.get('/org').status_code == 200
    assert c.get('/platform').status_code == 403


def test_member_can_open_kbs_but_not_org(app_with_db, pages_env):
    """🔴 成员看不到企业后台 —— 这是审查明确要求的。"""

    c = app_with_db.test_client()
    _login(c, 'Amember')

    assert c.get('/kbs').status_code == 200
    assert c.get('/org').status_code == 403, '🔴 成员不该进企业后台'
    assert c.get('/platform').status_code == 403


def test_viewer_can_open_kbs_but_not_org(app_with_db, pages_env):
    c = app_with_db.test_client()
    _login(c, 'Aviewer')

    assert c.get('/kbs').status_code == 200
    assert c.get('/org').status_code == 403
    assert c.get('/platform').status_code == 403


def test_platform_admin_can_open_platform(app_with_db, pages_env):
    c = app_with_db.test_client()
    _login(c, '平台管理员')

    assert c.get('/platform').status_code == 200
    # 平台管理员不属于任何租户，企业后台对它无意义
    assert c.get('/org').status_code == 403


def test_denied_page_has_clear_message(app_with_db, pages_env):
    """403 时给的是一个人能看懂的页面，不是一片空白。"""

    c = app_with_db.test_client()
    _login(c, 'Amember')

    body = c.get('/org').get_data(as_text=True)

    assert '无权访问' in body
    assert '仅企业管理员' in body


# ============ 前端菜单与服务端规则一致 ============
def test_menu_rules_match_server_rules():
    """🔴 菜单显示什么和服务端放行什么必须是同一套规则。

    不一致会导致「点得进去但 403」或「明明有权限却看不到入口」。
    """

    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    base_html = (root / 'templates' / 'base.html').read_text(encoding='utf-8')
    pages_py = (root / 'pages.py').read_text(encoding='utf-8')

    # 企业后台：菜单与服务端都用 manage_org_users
    assert "permissions.includes('manage_org_users')" in base_html
    assert "has_permission(user, 'manage_org_users')" in pages_py

    # 平台后台：都用 cross_tenant
    assert "permissions.includes('cross_tenant')" in base_html
    assert "has_permission(user, 'cross_tenant')" in pages_py


# ============ 不引 CDN / 不引前端框架 ============
def test_no_cdn_references_in_templates():
    """🔴 不许引 CDN。

    RMB03 踩过：CDN 拉不到时图表静默空白，页面上什么都看不到
    但也不报错 —— 演示时最尴尬。
    """

    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / 'templates'
    pattern = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.I)

    offenders = []
    for path in root.glob('*.html'):
        text = path.read_text(encoding='utf-8')
        if pattern.search(text):
            offenders.append(path.name)

    assert not offenders, f'🔴 这些模板引了外部 URL（CDN）：{offenders}'


def test_no_frontend_framework_references():
    """🔴 不许引 React/Vue 等框架 —— 审查明确要求 Flask 模板 + 原生 fetch。"""

    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / 'templates'
    banned = ['react', 'vue.js', 'vue.min', 'angular', 'jquery', 'svelte']

    for path in root.glob('*.html'):
        text = path.read_text(encoding='utf-8').lower()
        for word in banned:
            assert word not in text, f'🔴 {path.name} 引了 {word}'


def test_all_page_templates_exist():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / 'templates'
    for name in ['base.html', 'login.html', 'kbs.html', 'org.html',
                 'platform.html', 'denied.html']:
        assert (root / name).is_file(), f'缺模板 {name}'


# ============ 页面数量（对照审查的"2 个页面"）============
def test_page_route_count(app_with_db):
    """🔴 审查打回时说「2 个页面（其中 1 个是 XM03 旧页）」。

    现在应有 6 个非 API 路由：/ /login /kbs /org /platform /healthz
    """

    pages = [
        str(rule) for rule in app_with_db.url_map.iter_rules()
        if not str(rule).startswith('/api') and not str(rule).startswith('/static')
    ]

    assert len(pages) >= 6, f'页面路由只有 {len(pages)} 个：{sorted(pages)}'
    for expected in ['/', '/login', '/kbs', '/org', '/platform', '/healthz']:
        assert expected in pages, f'缺路由 {expected}'


def test_old_quiz_page_not_root_anymore(app_with_db):
    """🔴 旧的题库页不能再是首页。

    它调的是 /api/ask（固定题库），和知识库产品无关。
    挂在 `/` 会让评审看到错误的首页。
    """

    c = app_with_db.test_client()
    r = c.get('/', follow_redirects=False)

    # 未登录跳登录页，绝不能直接 200 渲染旧题库页
    assert r.status_code == 302
    assert '题库问答' not in r.get_data(as_text=True)