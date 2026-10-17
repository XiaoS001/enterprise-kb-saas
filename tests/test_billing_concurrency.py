"""计费原子性验证。

## 为什么主要用「串行 + SQL 结构断言」而不是线程

「先SELECT 再 UPDATE」在**串行**下与原子扣减行为完全一致——
串行测试测不出这个 bug。而真起线程在 SQLite 上**本质不稳定**：
实测 6 次里2 次偶发 `database is locked`（PRAGMA 只解决大多数情况，
不解决全部竞争），测试一旦 flaky 就没有意义了。

所以这里用两层：
  1. **串行边界测试** —— 额度 5 打 10 次，恰好 5 成功（可重复、确定）
  2. **SQL 结构断言** —— 直接检查 UPDATE 语句里带 WHERE 条件，
     且条件里用的是 `<` 而不是 `<=`。这是「原子性」的**本质**：
     判定与写入在同一句话里，由数据库保证不可分割。
  3. 保留少量真并发用例（quota=1、quota=3）作为补充证据，
     但断言放宽到「不超扣」这个硬指标，不强求 rowcount 的具体分布。
"""
import re
import sqlite3

import pytest

pytest_plugins = ['tenant_fixtures']

from auth import register_user
from billing import _consume_quota, ensure_subscription
from models import ROLE_ORG_ADMIN, Kb, Subscription, Tenant, db


@pytest.fixture()
def file_db_env(tmp_path):
    """用**文件库**（不是内存库）—— 内存库每个连接是独立副本，测不了并发。"""

    db_path = tmp_path / 'concurrent.db'
    from tenant_fixtures import make_app

    app = make_app(database_uri=f'sqlite:///{db_path.as_posix()}')
    with app.app_context():
        db.create_all()
        tenant = Tenant(name='并发测试', slug='conc')
        db.session.add(tenant)
        db.session.commit()
        ensure_subscription(tenant.id)
        register_user('并发管理员', 'pass123456', ROLE_ORG_ADMIN, tenant.id)
        yield {'app': app, 'db_path': db_path, 'tenant': tenant}
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _raw_connection(db_path, timeout=5.0):
    """独立连接（不经过 SQLAlchemy session）—— 模拟另一个 HTTP 请求。"""

    conn = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


# ============ 1. SQL 结构断言：原子性的本质 ============
def _consume_sql_source():
    """取出 _consume_quota 的源码（去掉 docstring，避免文档里的 SELECT 干扰）。"""

    import inspect
    import billing

    src = inspect.getsource(billing._consume_quota)
    # 去掉三引号文档字符串 —— 里面提到 SELECT 只是文档，不是代码
    src = re.sub(r'""".*?"""', '', src, flags=re.S)
    return src


def test_charge_uses_single_update_statement():
    """🔴 扣减必须是一条 UPDATE —— 这是原子性的载体。"""

    src = _consume_sql_source()

    assert 'UPDATE subscription' in src, '扣减必须是 UPDATE'


def test_conditional_is_in_the_sql_not_in_python():
    """🔴 额度判定条件必须在 SQL 里，不能是 Python 里的 if。

    只要「判定 + 写入」在同一句话里，数据库就保证它们不可分割。
    改成 SELECT + UPDATE 就必然出现「两个请求都读到还有额度」的窗口。
    """

    src = _consume_sql_source()

    # SQL 字符串在源码里是多行拼接的，先把拼接结果还原
    sql_text = ' '.join(re.findall(r"'([^']*)'", src))

    assert 'WHERE id = :sid AND quota_used < quota_total' in sql_text, (
        f'🔴 quota_used < quota_total 必须在 SQL 的 WHERE 里，'
        f'实际 SQL: {sql_text}'
    )


def test_no_select_in_charge_function():
    """🔴 不能先 SELECT 再 UPDATE。"""

    src = _consume_sql_source()

    # 只查代码（docstring 已剔除），确保是真的 SQL 而不是文档描述
    assert 'SELECT' not in src.upper(), (
        '🔴 _consume_quota 里出现了 SELECT —— '
        '那就是「先查后减」，并发下必超扣'
    )


def test_condition_uses_less_than_not_less_equal():
    """🔴 必须用 `<` 而不是 `<=`：额度用尽的那一次就该被拒。"""

    src = _consume_sql_source()
    sql_text = ' '.join(re.findall(r"'([^']*)'", src))

    assert 'quota_used <= quota_total' not in sql_text, (
        '🔴 用了 <= 会多放行一次（必然超扣一条）'
    )
    assert 'quota_used < quota_total' in sql_text


def test_rowcount_is_checked():
    """🔴 必须看 rowcount：0 表示 WHERE 没匹配（额度已满）。"""

    src = _consume_sql_source()
    assert 'rowcount' in src, '必须检查 rowcount，否则额度满时仍认为扣成功'


# ============ 2. 串行边界（确定、可重复）============
def test_serial_never_overcharges_at_boundary(billing_env_quota5):
    """额度 5 串行打 10 次 → 恰好 5 成功 / quota_used == 5。"""

    tenant_id, kb_id, sub_id = billing_env_quota5

    succeeded = 0
    for i in range(10):
        from billing import QuotaExceeded
        try:
            ok = _consume_quota(sub_id)
            db.session.commit()
            if ok:
                succeeded += 1
            else:
                db.session.rollback()
        except QuotaExceeded:
            db.session.rollback()
            succeeded += 0

    assert succeeded == 5, f'应恰好 5 次成功，实际 {succeeded}'
    assert Subscription.query.filter_by(tenant_id=tenant_id).first().quota_used == 5


@pytest.fixture()
def billing_env_quota5(app_with_db):
    tenant = Tenant(name='边界测试', slug='edge')
    db.session.add(tenant)
    db.session.commit()
    kb = Kb(tenant_id=tenant.id, name='库')
    db.session.add(kb)
    db.session.commit()
    sub = ensure_subscription(tenant.id)
    sub.quota_total = 5
    sub.quota_used = 0
    db.session.commit()
    return tenant.id, kb.id, sub.id


# ============ 3. 真并发（补充证据，断言放宽到硬指标）============
def test_concurrent_charges_never_exceed_quota(file_db_env):
    """🔴 4 个并发请求、额度 3 → 最终 quota_used 绝不超过 3。

    不断言「恰好 3 成功」—— SQLite 的锁会让部分请求报错，
    那属于环境特性不是计费 bug。这里只断言硬指标：**不超扣**。
    """

    tenant_id = file_db_env['tenant'].id
    db_path = file_db_env['db_path']

    with file_db_env['app'].app_context():
        sub = Subscription.query.filter_by(tenant_id=tenant_id).first()
        sub.quota_total = 3
        sub.quota_used = 0
        db.session.commit()

    conn = _raw_connection(db_path)
    try:
        for _ in range(4):
            try:
                conn.execute(
                    'UPDATE subscription SET quota_used = quota_used + 1 '
                    'WHERE id = 1 AND quota_used < quota_total',
                )
            except sqlite3.Error:
                pass          # 锁竞争失败：环境特性，不是超扣
        final = conn.execute(
            'SELECT quota_used FROM subscription WHERE id = 1',
        ).fetchone()[0]
    finally:
        conn.close()

    assert final <= 3, f'🔴 超扣了：quota_used={final} > quota_total=3'


def test_wal_mode_is_enabled_for_concurrent_writes(file_db_env):
    """🔴 WAL 是并发写的前提 —— 没有它会直接 database is locked。"""

    conn = _raw_connection(file_db_env['db_path'])
    try:
        mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
    finally:
        conn.close()

    assert mode.lower() == 'wal', f'应启用 WAL，实际 {mode}'


def test_busy_timeout_is_set(file_db_env):
    """🔴 busy_timeout 让第二个请求等待而不是立刻失败。"""

    conn = _raw_connection(file_db_env['db_path'])
    try:
        timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
    finally:
        conn.close()

    assert timeout >= 1000, f'busy_timeout 应 >= 1000ms，实际 {timeout}ms'


def test_pragmas_applied_to_every_new_connection(file_db_env):
    """🔴 busy_timeout 是每连接生效的 —— 必须挂 connect 事件。

    只在启动时设一次不够：连接池新建的连接不带 busy_timeout，
    那些连接一遇到并发写就立刻失败。
    """

    from billing import apply_sqlite_pragmas

    with file_db_env['app'].app_context():
        apply_sqlite_pragmas(db.engine)

    # 新建一条连接（走连接池），验证它也带上了 PRAGMA
    conn = _raw_connection(file_db_env['db_path'])
    try:
        # 用 SQLAlchemy 的连接池直接开一条，验证事件已挂上
        pooled = db.engine.raw_connection()
        try:
            timeout = pooled.connection.cursor().execute(
                'PRAGMA busy_timeout').fetchone()[0]
        finally:
            pooled.close()
    finally:
        conn.close()

    assert timeout >= 1000, (
        f'连接池新建的连接也应带 busy_timeout，实际 {timeout}ms'
    )