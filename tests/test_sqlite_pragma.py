"""SQLite PRAGMA 初始化与失败可见性测试。

🔴 这组测试的来源是审查提的一个问题：

    「新建临时库时 WAL 设置失败可能是正常的，但要确认：失败后有没有
      降级提示？会不会静默变成不用 WAL？」

实测答案：**会**。原实现是

    try:
        apply_sqlite_pragmas(db.engine)
    except Exception:
        application.logger.warning('sqlite_pragmas_failed')   # ← 只留一句话

两个问题：
  1. 真实异常被丢弃，排查时完全不知道失败原因；
  2. 静默降级成「不用 WAL」—— 并发写会锁库，调用方毫不知情。

而且失败的根本原因更蠢：原代码在 `app_context` **外面**调 `db.engine`，
Flask-SQLAlchemy 的 engine 绑定在应用上下文上，所以**从来就没成功过**。
那句 warning 每次启动都在打，但没人注意到 —— 因为它的日志里没有信息。
"""
import logging

import pytest

pytest_plugins = ['tenant_fixtures']

from models import db


@pytest.fixture()
def file_db_app(tmp_path):
    from tenant_fixtures import make_app

    db_path = tmp_path / 'pragma.db'
    app = make_app(database_uri=f'sqlite:///{db_path.as_posix()}')
    return {'app': app, 'db_path': db_path}


# ============ 1. PRAGMA 真的生效了 ============
def test_wal_mode_enabled_on_startup(file_db_app):
    """🔴 启动后 journal_mode 必须是 wal。

    原实现因为调用位置错了（在 app_context 外），这一条**从来就没通过过**。
    """

    with file_db_app['app'].app_context():
        mode = db.session.execute(db.text('PRAGMA journal_mode')).scalar()

    assert str(mode).lower() == 'wal', f'期望 wal，实际 {mode}'


def test_busy_timeout_enabled_on_startup(file_db_app):
    with file_db_app['app'].app_context():
        timeout = db.session.execute(db.text('PRAGMA busy_timeout')).scalar()

    assert timeout >= 1000, f'期望 >= 1000，实际 {timeout}'


def test_pragma_applies_to_new_pooled_connection(file_db_app):
    """🔴 busy_timeout 是每连接生效的 —— 连接池新建的连接也要带上。"""

    with file_db_app['app'].app_context():
        raw = db.engine.raw_connection()
        try:
            cursor = raw.connection.cursor()
            timeout = cursor.execute('PRAGMA busy_timeout').fetchone()[0]
        finally:
            raw.close()

    assert timeout >= 1000, f'连接池新连接的 busy_timeout = {timeout}'


# ============ 2. 失败必须可见（不能静默吞）============
def test_pragma_failure_is_not_silently_swallowed(monkeypatch, caplog, tmp_path):
    """🔴 PRAGMA 设置失败时，日志里必须有**异常类型与原文**。

    只打一句 'sqlite_pragmas_failed' 等于没打 —— 排查时看不到原因。
    """

    from app import create_app
    import billing

    def boom(_engine):
        raise RuntimeError('模拟 WAL 不可用')

    monkeypatch.setattr(billing, 'apply_sqlite_pragmas', boom)

    caplog.set_level(logging.DEBUG)
    create_app(overrides={
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{(tmp_path / "x.db").as_posix()}',
    })

    warnings = [r for r in caplog.records if 'sqlite_pragmas_failed' in r.getMessage()]
    assert warnings, '失败时必须有日志'

    text = ' '.join(r.getMessage() for r in warnings)
    assert 'RuntimeError' in text, f'日志应含异常类型，实际：{text}'
    assert '模拟 WAL 不可用' in text, f'日志应含异常原文，实际：{text}'


def test_pragma_state_is_logged_as_evidence(file_db_app, caplog):
    """🔴 无论成功失败，都要记录 PRAGMA 的**实际生效值**。

    这才是能定位问题的证据：「attempted wal but got delete」比「失败了」有用得多。
    """

    caplog.set_level(logging.INFO)
    with file_db_app['app'].app_context():
        from app import _setup_sqlite_and_tables
        _setup_sqlite_and_tables(file_db_app['app'])

    joined = ' '.join(r.getMessage() for r in caplog.records)
    assert 'sqlite_pragma_state' in joined, f'应记录实际值，实际日志：{joined}'
    assert 'journal_mode=' in joined


def test_wal_unavailable_emits_explicit_warning(monkeypatch, caplog, tmp_path):
    """🔴 WAL 没生效时要明确警告「并发会锁库」，不能只是状态正常。

    实测内存库的 journal_mode 是 'memory' 而不是 'wal' ——
    这属于「WAL 不可用」的真实场景，必须有提示。
    """

    from app import create_app
    import billing

    # 让 pragma「成功」但实际不会变成 wal（内存库就是这样）
    monkeypatch.setattr(billing, 'apply_sqlite_pragmas', lambda engine: None)

    caplog.set_level(logging.DEBUG)
    create_app(overrides={
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
    })

    joined = ' '.join(r.getMessage() for r in caplog.records)
    assert 'sqlite_wal_unavailable' in joined, (
        f'WAL 不可用时应明确警告，实际日志：{joined}'
    )
    assert 'PostgreSQL' in joined, '应给出替代方案'


# ============ 3. 结构断言：调用必须在 app_context 内 ============
def test_setup_called_inside_app_context():
    """🔴 防回归：db.engine 必须在 app_context 内使用。

    原 bug 就是在外面调，导致每次启动都失败但没人发现。
    这条用源码扫描盯住结构。
    """

    import inspect
    import app as app_module

    src = inspect.getsource(app_module.create_app)

    assert 'with application.app_context():' in src
    # _setup_sqlite_and_tables 的调用必须出现在 with 块之后
    ctx_pos = src.index('with application.app_context():')
    call_pos = src.index('_setup_sqlite_and_tables(application)')
    assert call_pos > ctx_pos, '_setup_sqlite_and_tables 必须在 app_context 内调用'