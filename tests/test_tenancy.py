pytest_plugins = ['tenant_fixtures']
"""多租户隔离测试（阶段 2 核心）。

按补充指示的「密度标准」写，三条硬标准：

  标准 1：6 张表，每张至少 1 条跨租户读测试
  标准 2：写侧也要测 —— 请求体里塞tenant_id 必须被忽略
  标准 3：每条都要断言「查到了几条」，不能只断言 is None
          （只写"查不到"的话，scoped() 恒返回空也是全绿 —— 假绿）

变异测试预期（我会自测）：
  ①删掉 scoped 的 tenant 过滤      → ≥8 条红
  ② scoped 恒用tenant_id=1        → ≥4 条红
  ③ scoped 恒返回空                → 一片红（靠标准 3 的"查得到"）
"""
import pytest
from flask import Flask

import config
from auth import hash_password, register_user
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
from tenancy import (
    TenantIsolationError,
    platform_scope,
    reset_tenant_context,
    scoped,
    set_tenant_context,
    tenant_fields,
)


# app / svc_ctx 等 fixture 统一在 conftest_app.py 定义
# （避开 XM03 自带 conftest.py 里的 client fixture 撞名）

@pytest.fixture()
def tenants(svc_ctx):
    """两个租户 + 各自的完整数据。"""

    a = Tenant(name='甲公司', slug='tenant-a')
    b = Tenant(name='乙公司', slug='tenant-b')
    db.session.add_all([a, b])
    db.session.commit()

    for tenant, tag in ((a, 'A'), (b, 'B')):
        db.session.add_all([
            Kb(tenant_id=tenant.id, name=f'{tag}的知识库'),
            Document(tenant_id=tenant.id, kb_id=1, title=f'{tag}文档',
                     content=f'{tag}内容'),
            Usage(tenant_id=tenant.id, question=f'{tag}的问题'),
            Subscription(tenant_id=tenant.id, quota_total=100),
        ])
    # 让两边的 kb_id / document_id 确实不同
    db.session.commit()
    return a, b


@pytest.fixture()
def users(svc_ctx, tenants):
    """每租户一个管理员 + 一个成员，外加一个平台管理员。"""

    a, b = tenants
    created = []
    for tenant, tag in ((a, 'A'), (b, 'B')):
        org_admin, _ = register_user(f'{tag}管理员', 'pass123456',
                                    ROLE_ORG_ADMIN, tenant_id=tenant.id)
        member, _ = register_user(f'{tag}成员', 'pass123456',
                                  ROLE_MEMBER, tenant_id=tenant.id)
        created.extend([org_admin, member])
    platform, _ = register_user('平台管理员', 'pass123456',
                                ROLE_PLATFORM_ADMIN)
    created.append(platform)
    return created


# ============ 标准 3 的前提：用对的租户能查到 ============
# 这一组是「假绿防火墙」—— 如果 scoped 恒返回空，下面这组会红，
# 从而保证后面「查不到」的断言不是空转。

def test_own_tenant_kb_is_visible(svc_ctx, tenants):
    a, _ = tenants
    rows = scoped(Kb, a.id).all()
    assert len(rows) == 1, '自己的知识库必须能查到'


def test_own_tenant_documents_are_visible(svc_ctx, tenants):
    a, _ = tenants
    assert len(scoped(Document, a.id).all()) == 1


def test_own_tenant_usage_is_visible(svc_ctx, tenants):
    a, _ = tenants
    assert len(scoped(Usage, a.id).all()) == 1


def test_own_tenant_subscription_is_visible(svc_ctx, tenants):
    a, _ = tenants
    assert len(scoped(Subscription, a.id).all()) == 1


def test_own_tenant_users_are_visible(svc_ctx, users):
    org_admin_a = users[0]
    rows = scoped(User, org_admin_a.tenant_id).all()
    assert len(rows) >= 2, '本租户的用户必须能查到'


# ============ 标准 1：6 张表各至少 1 条跨租户读测试 ============

def test_kb_is_isolated_across_tenants(svc_ctx, tenants):
    """🔴 Kb：A 租户查不到 B 租户的知识库。"""

    a, b = tenants
    a_kb_id = scoped(Kb, a.id).first().id
    b_kb_id = scoped(Kb, b.id).first().id

    # 前提（标准 3）：用对的租户能查到自己的库
    assert scoped(Kb, a.id).filter_by(id=a_kb_id).first() is not None
    assert len(scoped(Kb, a.id).all()) == 1, 'A 只该看到自己的 1 个库'
    # 结论：拿 A 的租户去查 B 的库 id
    assert scoped(Kb, a.id).filter_by(id=b_kb_id).first() is None
    assert len(scoped(Kb, b.id).all()) == 1, 'B 也只该看到自己的 1 个库'


def test_document_is_isolated_across_tenants(svc_ctx, tenants):
    """🔴 Document：A 查不到 B 的文档。"""

    a, b = tenants
    a_doc_id = scoped(Document, a.id).first().id
    b_doc_id = scoped(Document, b.id).first().id

    # 前提：用对的租户能按 id 查到自己的
    assert scoped(Document, a.id).filter_by(id=a_doc_id).first() is not None
    assert len(scoped(Document, a.id).all()) == 1, 'A 只该看到自己的 1 篇'
    # 结论
    assert scoped(Document, a.id).filter_by(id=b_doc_id).first() is None


def test_usage_is_isolated_across_tenants(svc_ctx, tenants):
    """🔴 Usage：A 查不到 B 的用量流水。"""

    a, b = tenants
    a_usage_id = scoped(Usage, a.id).first().id
    b_usage_id = scoped(Usage, b.id).first().id

    # 前提：用对的租户能按 id 查到自己的
    assert scoped(Usage, a.id).filter_by(id=a_usage_id).first() is not None
    assert len(scoped(Usage, a.id).all()) == 1, 'A 只该看到自己的 1 条'
    # 结论
    assert scoped(Usage, a.id).filter_by(id=b_usage_id).first() is None


def test_subscription_is_isolated_across_tenants(svc_ctx, tenants):
    """🔴 Subscription：A 查不到 B 的套餐。"""

    a, b = tenants
    a_sub_id = scoped(Subscription, a.id).first().id
    b_sub_id = scoped(Subscription, b.id).first().id

    # 前提：用对的租户能按 id 查到自己的
    assert scoped(Subscription, a.id).filter_by(id=a_sub_id).first() is not None
    assert len(scoped(Subscription, a.id).all()) == 1, 'A 只该看到自己的 1 份'
    # 结论
    assert scoped(Subscription, a.id).filter_by(id=b_sub_id).first() is None


def test_user_is_isolated_across_tenants(svc_ctx, users):
    """🔴 User：A 租户查不到 B 租户的用户。"""

    a_admin = users[0]
    b_admin = users[2]

    # 前提：自己的用户按 id 查得到
    assert scoped(User, a_admin.tenant_id).filter_by(
        id=a_admin.id
    ).first() is not None
    a_rows = scoped(User, a_admin.tenant_id).all()
    assert len(a_rows) == 2, 'A 租户应有 2 个用户'
    assert all(u.tenant_id == a_admin.tenant_id for u in a_rows)
    # 结论：B 的用户查不到
    assert scoped(User, a_admin.tenant_id).filter_by(
        id=b_admin.id
    ).first() is None


def test_tenant_table_is_isolated(svc_ctx, tenants):
    """🔴 Tenant：租户上下文下只能看到自己那一个租户。

    ⚠️ 这条测试原来是同义反复 —— 只测了「带 id 精确查询能用」，
    没测「不带 id 列全部会看到什么」。实测发现真漏洞：
        租户上下文下 Tenant.query.all() 返回**全部租户**。
    因为 Tenant 的主键是 id，没有 tenant_id 列，被 TENANT_SCOPED_MODELS
    漏掉了。现在由 ORM 层的 _tenant_self_filter 兜住。
    """

    a, b = tenants

    set_tenant_context(a.id)
    try:
        rows = Tenant.query.all()
        assert len(rows) == 1, '🔴 租户上下文下只能看到自己'
        assert rows[0].id == a.id
        assert b.id not in [t.id for t in rows]
    finally:
        reset_tenant_context(None)


def test_platform_admin_can_list_all_tenants(svc_ctx, tenants):
    """平台管理员要看全部租户 —— 这是它的职责，不该被过滤。"""

    set_tenant_context(1, is_platform_admin=True)
    try:
        assert len(Tenant.query.all()) == 2
    finally:
        reset_tenant_context(None)


def test_scoped_rejects_tenant_model_with_clear_message(svc_ctx, tenants):
    """🔴 scoped(Tenant, ...) 必须给出**可操作的**错误提示。

    Tenant 没有 tenant_id 列，直接用会报 SQLAlchemy 的
    "Entity namespace has no property 'tenant_id'" —— 那个信息对调用者没用。
    """

    with pytest.raises(TenantIsolationError) as exc:
        scoped(Tenant, 1)

    message = str(exc.value)
    assert 'tenant_id 列' in message
    assert 'get_by_id' in message, '错误提示要告诉调用者该怎么改'


# ============ 第二道防线（ORM 层自动过滤）============
# 🔴 这三条针对的是「绕过 scoped() 直接写 Model.query」的场景。
#    没有它们的话，删掉整段 with_loader_criteria 代码，测试依然全绿 ——
#    保险从来不验等于不存在。

def test_orm_auto_filter_blocks_direct_query(svc_ctx, tenants):
    """🔴 绕过 scoped() 直接查，ORM 层仍必须过滤。"""

    a, _ = tenants

    set_tenant_context(a.id)
    try:
        rows = Kb.query.all()          # 故意不走 scoped
        assert len(rows) == 1, '🔴 直接写 Model.query 也必须被过滤'
        assert rows[0].tenant_id == a.id
    finally:
        reset_tenant_context(None)


def test_orm_auto_filter_skipped_without_context(svc_ctx, tenants):
    """无租户上下文时不过滤 —— CLI / 建表 / 造测试数据要能用。"""

    assert len(Kb.query.all()) == 2


def test_orm_auto_filter_skipped_for_platform_admin(svc_ctx, tenants):
    """平台管理员跨租户，不该被过滤。"""

    set_tenant_context(1, is_platform_admin=True)
    try:
        assert len(Kb.query.all()) == 2
    finally:
        reset_tenant_context(None)


def test_orm_auto_filter_covers_all_tenant_models(svc_ctx, tenants, users):
    """🔴 每张租户级表都要被自动过滤覆盖，不能漏一张。

    同时断言「查得到」与「查不全」：A 租户有 2 个用户却只能看到 1 个，
    证明 User 也被过滤了（不是恒不过滤，也不是恒返回空）。
    """

    a, _ = tenants
    set_tenant_context(a.id)
    try:
        assert len(Kb.query.all()) == 1
        assert len(Document.query.all()) == 1
        assert len(Usage.query.all()) == 1
        assert len(Subscription.query.all()) == 1
        # User 表：A、B 两租户共 4 人 + 1 个平台管理员，过滤后只剩 A 的 2 人
        # （平台管理员 tenant_id 为 NULL，本来就不属于任何租户）
        assert len(User.query.all()) == 2, '🔴 User 也必须被自动过滤'
        assert all(u.tenant_id == a.id for u in User.query.all())
    finally:
        reset_tenant_context(None)


# ============ scoped 的契约 ============

def test_scoped_with_none_tenant_raises(svc_ctx):
    """🔴 tenant_id=None 必须抛异常，不能静默不过滤。"""

    with pytest.raises(TenantIsolationError):
        scoped(Kb, None).all()


def test_scoped_none_tenant_error_mentions_danger(svc_ctx):
    """错误信息要说清「不过滤=全库可见」这个后果。"""

    with pytest.raises(TenantIsolationError) as exc:
        scoped(Usage, None)
    assert 'tenant_id' in str(exc.value)


def test_scoped_alone_isolates_without_orm_context(svc_ctx, tenants):
    """🔴🔴 **两道防线必须能各自独立被验证**。

    这是阶段 3 补的关键一条。之前 5 条跨租户测试都在**租户上下文内**跑，
    于是：
      · 第一道 scoped() 坏掉→ 第二道 ORM 自动过滤兜住 → 测试全绿
      · 第二道 ORM 过滤坏掉 → 第一道 scoped() 兜住 → 测试全绿
    **两道防线互为掩护，等于都没被验证过。**（变异①③④ 一度全 0 失败就是这个原因）

    这里显式**清空上下文**，强制只走 scoped 一条路径 ——
    这样「scoped 被破坏」就一定暴露。
    """

    a, b = tenants
    reset_tenant_context()          # ← 关键：关掉第二道防线

    # 第一道：scoped 必须自己就能隔离
    assert len(scoped(Kb, a.id).all()) == 1
    assert len(scoped(Kb, b.id).all()) == 1
    assert [k.name for k in scoped(Kb, a.id).all()] == ['A的知识库']

    for model in (Document, Usage, Subscription):
        assert len(scoped(model, a.id).all()) == 1, f'{model.__name__} 未被 scoped 隔离'


def test_scoped_alone_blocks_foreign_ids_without_context(svc_ctx, tenants):
    """🔴 同上：无上下文时 scoped 必须挡住跨租户按 id 查询。"""

    a, b = tenants
    reset_tenant_context()

    b_kb_id = scoped(Kb, b.id).first().id
    b_doc_id = scoped(Document, b.id).first().id

    assert scoped(Kb, a.id).filter_by(id=b_kb_id).first() is None
    assert scoped(Document, a.id).filter_by(id=b_doc_id).first() is None


def test_orm_layer_alone_isolates_without_scoped(svc_ctx, tenants):
    """🔴 反向：把 scoped 也旁路掉，只验证第二道防线。

    两条测试合起来 = 「任一道防线单独都足以隔离」，
    变异测试才能分别打穿其中一道。
    """

    a, _ = tenants

    set_tenant_context(a.id)
    try:
        # 完全不走 scoped，直接 Model.query
        assert len(Kb.query.all()) == 1
        assert len(Document.query.all()) == 1
        assert len(Usage.query.all()) == 1
        assert len(Subscription.query.all()) == 1
        assert len(Tenant.query.all()) == 1
    finally:
        reset_tenant_context()


def test_orm_layer_alone_blocks_foreign_ids_without_scoped(svc_ctx, tenants):
    """🔴 同上：第二道防线也要能挡住跨租户按 id 查。"""

    a, b = tenants
    b_kb_id = scoped(Kb, b.id).first().id

    set_tenant_context(a.id)
    try:
        assert Kb.query.filter_by(id=b_kb_id).first() is None
    finally:
        reset_tenant_context()


def test_scoped_returns_only_requested_tenant_count(svc_ctx, tenants):
    """数量断言：不只是"查不到别人的"，而是"正好只查到自己的"。"""

    a, b = tenants
    assert len(scoped(Kb, a.id).all()) == 1
    assert len(scoped(Kb, b.id).all()) == 1


def test_platform_scope_sees_every_tenant(svc_ctx, tenants):
    """平台管理员是跨租户角色 —— 显式走 platform_scope。"""

    assert len(platform_scope(Kb).all()) == 2


# ============ 标准 2：写侧隔离 ============

def test_tenant_fields_forces_current_tenant(svc_ctx, tenants):
    """🔴 请求体里的 tenant_id 必须被丢弃并强制设为当前租户。"""

    a, _ = tenants
    payload = tenant_fields(
        {'name': '新库', 'tenant_id': 999}, a.id,
    )
    assert payload['tenant_id'] == a.id, 'tenant_id 必须被强制覆盖'
    assert payload['name'] == '新库'


def test_tenant_fields_drops_forged_tenant(svc_ctx, tenants):
    """即使伪造了 tenant_id，落库的也是自己的租户。"""

    _, b = tenants
    payload = tenant_fields({'name': 'X', 'tenant_id': b.id}, 1)
    assert payload['tenant_id'] == 1, '不能被请求体里的租户覆盖'


def test_created_record_lands_in_own_tenant(svc_ctx, tenants):
    """🔴 端到端：带伪造 tenant_id 的请求，落库后仍在自己的租户。"""

    a, b = tenants
    payload = tenant_fields({'name': '上传的库', 'tenant_id': b.id}, a.id)
    kb = Kb(**payload)
    db.session.add(kb)
    db.session.commit()

    assert kb.tenant_id == a.id, '必须落在自己的租户'
    assert scoped(Kb, a.id).filter_by(id=kb.id).first() is not None
    assert scoped(Kb, b.id).filter_by(id=kb.id).first() is None


def test_document_upload_to_foreign_kb_is_rejected(svc_ctx, tenants):
    """🔴 给自己的库上传文档时传别人的 kb_id → 必须查不到。"""

    a, b = tenants
    own_kb = scoped(Kb, a.id).first()
    foreign_kb_id = scoped(Kb, b.id).first().id

    payload = tenant_fields(
        {'kb_id': foreign_kb_id, 'title': '偷渡', 'content': 'x'}, a.id,
    )
    # 写入前先确认该 kb 不在自己的可见范围
    assert scoped(Kb, a.id).filter_by(id=payload['kb_id']).first() is None
    assert own_kb.id != payload['kb_id']


def test_usage_record_cannot_target_foreign_tenant(svc_ctx, tenants):
    """🔴 写用量时指定别人的 tenant → 被强制覆盖。"""

    a, b = tenants
    payload = tenant_fields(
        {'question': '问题', 'tenant_id': b.id}, a.id,
    )
    usage = Usage(**payload)
    db.session.add(usage)
    db.session.commit()

    assert usage.tenant_id == a.id


# ============ 越权一律 404 而非 403 ============

def test_cross_tenant_lookup_returns_none_not_403_semantics(svc_ctx, tenants):
    """跨租户取不到 = 当作不存在；上层据此返 404。

    403 会泄漏「这个资源存在」，所以 fetch 语义是 None 而非抛异常。
    """

    a, b = tenants
    b_kb_id = scoped(Kb, b.id).first().id

    assert scoped(Kb, a.id).filter_by(id=b_kb_id).first() is None
