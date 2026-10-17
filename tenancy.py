"""🔴 多租户隔离 —— 本项目最重要的约束。

## 为什么不能靠自觉

手写 `.filter_by(tenant_id=...)` 一定会漏：新增一个接口忘了加，就是越权。
所以这里的做法是**让业务代码拿不到「未过滤」的 query**：

    scoped(Kb, tenant_id)   ← 唯一入口，返回已带过滤的 query
    Kb.query                ← 仍然可用（SQLAlchemy 的默认行为改不掉），
                               所以下面还有 loader_criteria 做第二道保险

## 两道防线

**第一道· `scoped()`** —— 显式过滤，可读、可测、变异测试打的就是它。

**第二道 · `with_loader_criteria`** —— 在 ORM 层给「当前租户上下文」里的
所有租户级模型自动加where 条件。效果是：**即使有人直接写 `Kb.query`，
只要请求上下文里有 current_tenant_id，过滤依然会生效。**

两道防线的分工：
- `scoped()` 是**显式契约**—— 代码里看得见，审查与变异测试针对它
- loader_criteria 是**兜底** —— 防止将来有人图省事直接写 `Model.query`

## 越权一律返 404

不返 403：403 会告诉攻击者「这个资源存在，只是你没权限」，等于泄漏信息。
"""
import contextvars

from sqlalchemy import event
from sqlalchemy.orm import with_loader_criteria

from models import TENANT_SCOPED_MODELS, db


# 当前请求的租户上下文。None 表示「无租户上下文」（CLI / 测试 / 后台任务）
current_tenant_id: contextvars.ContextVar = contextvars.ContextVar(
    'rmb01_current_tenant_id', default=None
)
current_is_platform_admin: contextvars.ContextVar = contextvars.ContextVar(
    'rmb01_current_is_platform_admin', default=False
)


def set_tenant_context(tenant_id, is_platform_admin=False):
    """进入租户上下文。返回 token，供 reset_tenant_context 用。"""

    t1 = current_tenant_id.set(tenant_id)
    t2 = current_is_platform_admin.set(bool(is_platform_admin))
    return (t1, t2)


def reset_tenant_context(tokens=None):
    """退出租户上下文。

    ⚠️ tokens 为 None 时**直接置空**，而不是「无 token 可恢复就什么都不做」。
    早期版本静默返回，导致测试里 `reset_tenant_context(None)` 看着生效了、
    实际没重置 —— 上下文泄漏到后续用例，造成一批莫名失败。

    两种用法都支持：
        tokens = set_tenant_context(tid); reset_tenant_context(tokens)  # 精确恢复
        set_tenant_context(tid); reset_tenant_context()                   # 直接清空
    """

    if tokens:
        current_tenant_id.reset(tokens[0])
        current_is_platform_admin.reset(tokens[1])
    else:
        current_tenant_id.set(None)
        current_is_platform_admin.set(False)


def get_tenant_id():
    return current_tenant_id.get()


def is_platform_admin():
    return current_is_platform_admin.get()


class TenantIsolationError(RuntimeError):
    """跨租户访问。

    🔴 统一抛这个而不是返回 None —— 让「忘了加过滤」变成显式失败，
    而不是静默返回空列表（静默的空列表最难查）。
    """


# ============ 第一道防线：显式 scoped() ============
def scoped(model, tenant_id):
    """🔴 所有租户级查询的唯一入口。

    返回已带tenant_id 过滤的 query。tenant_id 为 None 时抛异常而不是
    「不过滤」—— 因为「不过滤」就等于全库可见，是最危险的默认值。

    用法：
        scoped(Kb, session_tenant_id).filter_by(id=kb_id).first()

    ⚠️ Tenant 表**不能**用 scoped —— 它的主键是 id，没有 tenant_id 列。
       租户表本身的隔离靠第二道防线的 _tenant_self_filter，
       「查自己」请用 Tenant.query.get_by_id(tenant_id)。
    """

    from models import Tenant

    if model is Tenant:
        raise TenantIsolationError(
            'Tenant 表没有 tenant_id 列，不能用 scoped()。'
            '租户表本身的隔离由 ORM 层自动过滤保证（_tenant_self_filter）；'
            '查自己的租户请用 Tenant.query.get_by_id(tenant_id)。'
        )
    if tenant_id is None:
        raise TenantIsolationError(
            f'{model.__name__} 查询必须提供 tenant_id；'
            'None 会导致不过滤（全库可见），这是最危险的默认值'
        )
    return model.query.filter_by(tenant_id=tenant_id)


def platform_scope(model):
    """平台管理员的跨租户查询 —— 显式函数，不靠"忘了调scoped"。

    用它必须付出可见代价：调用处写的是 platform_scope 而不是 scoped，
    代码审查时一眼能看出「这里跨了租户」。
    """

    return model.query


# ============ 第二道防线：ORM 层自动过滤 ============
def _tenant_filter(model):
    """给模型构造 loader 条件：只加载当前租户的行。"""

    tenant_id = current_tenant_id.get()
    return with_loader_criteria(
        model,
        lambda cls: cls.tenant_id == tenant_id,
        include_aliases=True,
    )


def _tenant_self_filter(tenant_id):
    """Tenant 表的过滤条件。

    🔴 Tenant 的主键**就是 id**，它没有 tenant_id 列 ——
       所以不能放进 TENANT_SCOPED_MODELS（那样会报
       "Entity namespace for 'tenant' has no property 'tenant_id'"），
       必须在这里单独构造「id 等于当前租户」的条件。

       这个漏洞是实测发现的：租户上下文下直接写 Tenant.query.all()
       会返回**全部租户**。当时现有测试没抓到，是因为它只测了
       「带 id 精确查询」而没测「不带 id 列全部」。
    """

    from models import Tenant

    return with_loader_criteria(
        Tenant,
        lambda cls: cls.id == tenant_id,
        include_aliases=True,
    )


@event.listens_for(db.session, 'do_orm_execute')
def _auto_filter_tenant(state):
    """🔴 ORM 执行前自动追加租户过滤。

    只在「有租户上下文且非平台管理员」时生效：
      · 无上下文（CLI / 建表 / 测试造数据）→ 不过滤，否则什么都查不到
      · 平台管理员 → 不过滤，这是它的职责
    """

    if state.is_column_load or current_tenant_id.get() is None \
            or is_platform_admin():
        return

    tenant_id = current_tenant_id.get()
    # Tenant 单独处理（主键是 id，没有 tenant_id 列）
    state.statement = state.statement.options(
        _tenant_self_filter(tenant_id)
    )
    for model in TENANT_SCOPED_MODELS:
        state.statement = state.statement.options(
            _tenant_filter(model)
        )


# ============ 写侧隔离 ============
def tenant_fields(payload, tenant_id, *, ignore=('tenant_id',)):
    """🔴 从请求体取字段时**强制覆盖**租户相关字段。

    为什么不直接用 payload.get('tenant_id')：
        那等于让客户端决定写进哪个租户 —— 攻击者改个数字就穿过来了。

    ignore 里的字段（默认 tenant_id）会被**丢弃并强制设为当前租户**。
    这样即使请求体里塞了 tenant_id=B，落库的仍是 A 自己的。
    """

    data = dict(payload)
    for field in ignore:
        data.pop(field, None)
    data['tenant_id'] = tenant_id
    return data

def get_current_tenant_id():
    """🔴 取当前租户 id —— 独立函数名，避免与ContextVar 变量 current_tenant_id 混淆。

    （ContextVar 变量名必须保持 current_tenant_id，因为 set/reset 依赖它的
    身份；但业务代码需要一个「取值」的入口，同名会导致
    "'_contextvars.ContextVar' object is not callable"。）
    """

    return current_tenant_id.get()
