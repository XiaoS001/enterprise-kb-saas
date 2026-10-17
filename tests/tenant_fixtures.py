"""RMB01 多租户测试的 fixture。

🔴 **测试库隔离**：业务库是 data/rmb01.db，测试绝不能碰它。
每个用例一套独立的内存 SQLite（sqlite:///:memory:），与业务库完全无关。

⚠️ 两个 conftest 共存说明：
XM03 自带 tests/conftest.py（基线文件，只定义 client fixture，**不许改动**）。
同目录下同名文件的加载顺序不确定，会互相覆盖，所以这里把新fixture 命名为
svc / svc_ctx / svc_client —— 不与 XM03 的 client / app 冲突。
"""

import pytest
from flask import Flask

from models import db


@pytest.fixture()
def svc():
    """干净的 app + 独立内存库（避免与 XM03 的 fixture 撞名）。"""

    application = Flask(__name__)
    application.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY='test-secret',
        TESTING=True,
    )
    db.init_app(application)

    from tenancy import reset_tenant_context

    with application.app_context():
        db.create_all()
        yield application
        # 🔴 兜底清空租户上下文：某个用例设了上下文但没恢复时，
        #    不该泄漏到后续用例（那会造成一批莫名失败，且极难排查）。
        reset_tenant_context()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture()
def svc_ctx(svc):
    """只要 app context。"""

    with svc.app_context():
        yield


@pytest.fixture()
def svc_client(svc):
    return svc.test_client()

def make_app(database_uri='sqlite:///:memory:'):
    """用 create_app 工厂造一个带库的 app。

    阶段 3 起真正的 app 由 app.create_app() 创建（接了 db），
    测试通过 overrides 注入内存库 —— 这样测的是**真实的应用工厂**，
    而不是另起一个 Flask 对象，配置差异不会漏测。
    """

    from app import create_app

    return create_app(overrides={
        'SQLALCHEMY_DATABASE_URI': database_uri,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'TESTING': True,
        'SECRET_KEY': 'test-secret',
    })

@pytest.fixture()
def app_with_db():
    """用真实应用工厂造的 app（注入内存库）。

    与 svc 的区别：svc 是裸 Flask 对象，app_with_db 走app.create_app()，
    所以路由注册、配置装配都会被测到。
    """

    application = make_app()
    with application.app_context():
        yield application
        from tenancy import reset_tenant_context
        reset_tenant_context()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()