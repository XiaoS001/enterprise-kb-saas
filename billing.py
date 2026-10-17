"""计量与额度扣减。

## 核心约束：原子扣减

🔴 **绝不允许「先 SELECT 再UPDATE」** —— 两个并发请求会同时读到
quota_used=4（总额度5），然后都判定"还有额度"，都写回 5+1=6 → **超扣**。

正确做法是**单语句原子 UPDATE + 看 rowcount**：

    UPDATE subscription SET quota_used = quota_used + 1
     WHERE id = ? AND quota_used < quota_total

数据库层面保证「判定 + 扣减」是一个不可分割的操作：
`quota_used < quota_total` 这个条件在写入的同一时刻求值，
并发时只有一个事务能让它成立。

rowcount=1 → 扣成功；rowcount=0 → 额度不足。

## 账实相符

`Usage` 流水里被计费的条数 应等于 `Subscription.quota_used`。
这两边对不上是计费系统最典型的 bug，tests/test_billing.py 有恒等式测试盯着。

## 谁该被计费

| 情形 | 计费 | 理由 |
|---|---|---|
| 正常问答（模型真的生成了答案） | ✅ | 消耗了模型额度 |
| **降级问答**（无 key / 模型失败） | 🔴 **不扣** | 没调模型，没消耗额度 |
| 检索未命中 | ❌ 不扣 | 没生成任何东西 |
| 参数错误 | ❌ 不扣 | 压根没开始 |

🔴 这条直接依赖阶段 4 前置修复 —— agent 的 degraded 标记曾经硬编码 False，
那会把降级问答当成正常问答计费，等于**收了钱没提供服务**。
"""
import logging
import sqlite3

from sqlalchemy import text

from models import Subscription, Usage, db


logger = logging.getLogger('rmb01.billing')


# 三档套餐
PLANS = {
    'free': {
        'name': '免费版',
        'quota_total': 100,
        'price_cny_month': 0,
        'features': ['5 个知识库', '每日 100 次问答', '7 天报表留存'],
    },
    'pro': {
        'name': '专业版',
        'quota_total': 10_000,
        'price_cny_month': 299,
        'features': ['50 个知识库', '每日 10000 次问答', '90 天报表', 'webhook 通知'],
    },
    'enterprise': {
        'name': '企业版',
        'quota_total': 100_000,
        'price_cny_month': 2999,
        'features': ['不限知识库', '每日 100000 次问答', '1 年报表', '私有化部署', '专属支持'],
    },
}

DEFAULT_PLAN = 'free'


class QuotaExceeded(Exception):
    """额度用完。

    🔴 单独一个异常类型：让调用方能明确区分「额度问题」（返 403 + 明确提示）
    与「系统故障」（返 500）。额度用完不是 500。
    """

    def __init__(self, plan, used, total):
        super().__init__(f'本期额度已用完（{used}/{total}），可升级套餐')
        self.plan = plan
        self.used = used
        self.total = total


def apply_sqlite_pragmas(engine):
    """SQLite 并发设置：WAL + busy_timeout。

    不设 WAL 的话，两个请求同时写会直接 database is locked；
    不设 busy_timeout 的话，第二个请求会立刻失败而不是等待。

    WAL 的关键收益：读写不互相阻塞 —— 读者不阻塞写者，写者不阻塞读者。

    busy_timeout 是**每连接**生效的。SQLAlchemy 的连接池会新建连接，
    只在启动时设一次不够 —— 所以挂 connect 事件，让**每条新连接**都带上。
    """

    if engine.url.get_backend_name() != 'sqlite':
        return

    def _set_on_connect(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA busy_timeout=5000')
        finally:
            cursor.close()

    from sqlalchemy import event
    event.listen(engine, 'connect', _set_on_connect)

    with engine.connect() as conn:
        conn.execute(text('PRAGMA journal_mode=WAL'))
        conn.execute(text('PRAGMA busy_timeout=5000'))
        conn.commit()

def ensure_subscription(tenant_id, plan=DEFAULT_PLAN):
    """确保租户有订阅（没有就按默认档创建）。返回 Subscription。"""

    existing = Subscription.query.filter_by(tenant_id=tenant_id).first()
    if existing is not None:
        return existing

    spec = PLANS.get(plan, PLANS[DEFAULT_PLAN])
    sub = Subscription(
        tenant_id=tenant_id,
        plan=plan if plan in PLANS else DEFAULT_PLAN,
        quota_total=spec['quota_total'],
        quota_used=0,
    )
    db.session.add(sub)
    db.session.commit()
    return sub


class PlanDowngradeConflict(Exception):
    """降级到更小额度，但已用量已超过新额度。

    🔴 单独一个异常类型：调用方要能明确区分「业务上不可降级」（返回 409 + 说明）
    与「系统故障」（500）。这不是程序错误，是需要人决策的业务冲突。
    """


def change_plan(tenant_id, new_plan):
    """变更套餐。返回 Subscription。

    🔴 **降级时的边界处理**（审查变异②指出的场景）：
    租户从 pro（10000）降到 free（100），但本月已用 5000。
    此时若直接改 quota_total=100，就出现 `quota_used(5000) > quota_total(100)`：
      · quota_remaining 变成负数
      · 之后每次扣减都会被 WHERE 拒绝 → 租户**永久锁死**，即使升级回来也要先修复数据
      · 账单口径也乱了

    所以：**已用量超过新额度时直接拒绝降级**，由运营决定
    「先清零重新计费周期」还是「让客户继续用完」。
    不做「自动把已用量截断成新额度」—— 那等于凭空抹掉客户已消耗的额度，
    对客户不公平，也会让账实不符。

    升级则总是允许（额度只会变大）。
    """

    spec = PLANS.get(new_plan)
    if spec is None:
        raise ValueError(f'未知套餐: {new_plan}')

    subscription = ensure_subscription(tenant_id)
    new_total = spec['quota_total']

    if new_total < subscription.quota_total and subscription.quota_used >= new_total:
        raise PlanDowngradeConflict(
            f'当前套餐已用 {subscription.quota_used} 次，'
            f'超过 {spec["name"]}的 {new_total} 次额度，'
            f'降级前需先处理已用量'
        )

    subscription.plan = new_plan
    subscription.quota_total = new_total
    db.session.commit()
    return subscription


def _consume_quota(subscription_id):
    """🔴 原子扣减：单语句 UPDATE + WHERE 条件判定。

    返回 True=扣成功，False=额度不足。

    ⚠️ 绝不能改成「先 SELECT 查 quota_used 再 UPDATE 写 quota_used+1」——
    那样并发下必超扣。`quota_used < quota_total` 必须留在 SQL 里，
    让数据库在写入的同一时刻求值这个条件。
    """

    result = db.session.execute(
        text(
            'UPDATE subscription '
            'SET quota_used = quota_used + 1 '
            'WHERE id = :sid AND quota_used < quota_total'
        ),
        {'sid': subscription_id},
    )
    # 🔴 必须看 rowcount：0 表示 WHERE 没匹配（额度已满），不是「执行成功」
    return result.rowcount == 1


def record_usage(tenant_id, kb_id, question, tokens=0, degraded=False,
                 user_id=None):
    """记一笔用量。**降级的问答不扣额度**。返回 (usage, error)。

    返回值约定：
      · 成功 → (Usage, None)
      · 额度不足 → (None, QuotaExceeded)
      · 参数问题 → (None, '错误信息')
    """

    if degraded:
        # 🔴 降级：没调模型 = 没消耗额度。流水仍记一笔（degraded=True），
        #    便于运营看到「有多少次是降级响应」，但不扣 quota_used。
        usage = Usage(
            tenant_id=tenant_id,
            kb_id=kb_id,
            user_id=user_id,
            question=question[:500],
            tokens=0,
            degraded=True,
        )
        db.session.add(usage)
        db.session.commit()
        return usage, None

    subscription = Subscription.query.filter_by(tenant_id=tenant_id).first()
    if subscription is None:
        return None, '该租户没有订阅，无法计量'

    if not _consume_quota(subscription.id):
        db.session.rollback()
        current = db.session.get(Subscription, subscription.id)
        raise QuotaExceeded(
            current.plan, current.quota_used, current.quota_total,
        )

    usage = Usage(
        tenant_id=tenant_id,
        kb_id=kb_id,
        user_id=user_id,
        question=question[:500],
        tokens=tokens,
        degraded=False,
    )
    db.session.add(usage)
    db.session.commit()
    return usage, None


def quota_status(tenant_id):
    """查询额度状态（不扣减）。"""

    sub = Subscription.query.filter_by(tenant_id=tenant_id).first()
    if sub is None:
        return None
    return {
        'plan': sub.plan,
        'plan_name': PLANS.get(sub.plan, {}).get('name', sub.plan),
        'quota_total': sub.quota_total,
        'quota_used': sub.quota_used,
        'quota_remaining': max(0, sub.quota_total - sub.quota_used),
        'exhausted': sub.quota_used >= sub.quota_total,
    }


def usage_summary(tenant_id, days=30):
    """用量汇总。区分正常与降级 —— 降级占比高说明该换 key 或升配了。"""

    since = db.session.query(Usage).filter(
        Usage.tenant_id == tenant_id,
    ).count()

    degraded_count = db.session.query(Usage).filter(
        Usage.tenant_id == tenant_id,
        Usage.degraded.is_(True),
    ).count()

    total_tokens = db.session.query(db.func.coalesce(
        db.func.sum(Usage.tokens), 0,
    )).filter(Usage.tenant_id == tenant_id).scalar()

    return {
        'period_days': days,
        'total_queries': since,
        'billable_queries': since - degraded_count,
        'degraded_queries': degraded_count,
        'total_tokens': int(total_tokens or 0),
    }
