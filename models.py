"""RMB01 数据模型 —— 多租户 SaaS。

🔴 **多租户隔离是本项目最重要的一条约束**，所以模型层的设计围绕它：

1. 六张租户级表都带 `tenant_id`，且建了索引 —— 隔离过滤是每条查询都要做的事，
   没有索引等于每查一次全表扫。
2. `Document` 同时带 `kb_id` 与 `tenant_id`：kb_id 用于定位，tenant_id 用于隔离。
   **两者都存是刻意的** —— 只存 kb_id 的话每次都要 join kb 表才能过滤，
   而 join 是最容易漏掉 where 条件的地方。
3. `tenant_id` 在模型上是普通列，但所有查询必须经 `scoped()`（见 tenancy.py）。

角色四档（任务单要求）：
    platform_admin  平台管理员：跨租户
    org_admin       企业管理员：本租户全权
    member          成员：只能问答
    viewer          只读：只能看历史
"""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


# --- 角色取值 ---
ROLE_PLATFORM_ADMIN = 'platform_admin'
ROLE_ORG_ADMIN = 'org_admin'
ROLE_MEMBER = 'member'
ROLE_VIEWER = 'viewer'
ALL_ROLES = (ROLE_PLATFORM_ADMIN, ROLE_ORG_ADMIN, ROLE_MEMBER, ROLE_VIEWER)

ROLE_LABELS = {
    ROLE_PLATFORM_ADMIN: '平台管理员',
    ROLE_ORG_ADMIN: '企业管理员',
    ROLE_MEMBER: '成员',
    ROLE_VIEWER: '只读',
}


class Tenant(db.Model):
    """租户（一家企业）。

    🔴 平台自身也是一个租户吗？**不是**。平台管理员是跨租户角色，
    不属于任何租户，所以 User.tenant_id 对平台管理员为 NULL。
    """

    __tablename__ = 'tenant'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(100), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    def __repr__(self):
        return f'<Tenant {self.id} {self.slug}>'


class User(db.Model):
    """用户。密码只存 hash，绝不存明文。"""

    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    # 🔴 nullable：平台管理员不属于任何租户
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey('tenant.id'),
        nullable=True,
        index=True,
    )
    username = db.Column(db.String(100), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    tenant = db.relationship('Tenant', back_populates='users')
    documents = db.relationship(
        'Document', back_populates='uploader', lazy='select'
    )

    def __repr__(self):
        return f'<User {self.id} {self.username} {self.role}>'


class Kb(db.Model):
    """知识库。一家租户可以有多个库。"""

    __tablename__ = 'kb'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey('tenant.id'),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    tenant = db.relationship('Tenant', back_populates='kbs')
    documents = db.relationship(
        'Document',
        back_populates='kb',
        cascade='all, delete-orphan',
        lazy='select',
    )

    def __repr__(self):
        return f'<Kb {self.id} {self.name}>'


class Document(db.Model):
    """文档（已分段）。

    🔴 同时存 kb_id 与 tenant_id：kb_id 定位、tenant_id 隔离。
    只存 kb_id 会让过滤依赖 join，而 join 是最容易漏 where 的地方。
    """

    __tablename__ = 'document'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey('tenant.id'),
        nullable=False,
        index=True,
    )
    kb_id = db.Column(
        db.Integer,
        db.ForeignKey('kb.id'),
        nullable=False,
        index=True,
    )
    uploader_id = db.Column(
        db.Integer,
        db.ForeignKey('user.id'),
        nullable=True,
    )
    title = db.Column(db.String(300), nullable=False)
    # 分段序号：同一文档的第几段
    chunk_index = db.Column(db.Integer, default=0, nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    kb = db.relationship('Kb', back_populates='documents')
    uploader = db.relationship('User', back_populates='documents')

    def __repr__(self):
        return f'<Document {self.id} kb={self.kb_id} #{self.chunk_index}>'


class Usage(db.Model):
    """用量流水（一次问答一行）。"""

    __tablename__ = 'usage'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey('tenant.id'),
        nullable=False,
        index=True,
    )
    kb_id = db.Column(
        db.Integer,
        db.ForeignKey('kb.id'),
        nullable=True,
        index=True,
    )
    user_id = db.Column(db.Integer, nullable=True)
    question = db.Column(db.String(500), nullable=False)
    tokens = db.Column(db.Integer, default=0, nullable=False)
    degraded = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now,
                           nullable=False, index=True)

    def __repr__(self):
        return f'<Usage {self.id} tenant={self.tenant_id}>'


class Subscription(db.Model):
    """订阅套餐与额度。

    🔴 quota_used 的扣减必须用**单语句原子 UPDATE**（阶段 4 实现），
    不许「先 SELECT 再 UPDATE」—— 并发下必超扣。
    这里只定义结构与约束，扣减逻辑在 billing.py。
    """

    __tablename__ = 'subscription'

    PLAN_FREE = 'free'
    PLAN_PRO = 'pro'
    PLAN_ENTERPRISE = 'enterprise'
    ALL_PLANS = (PLAN_FREE, PLAN_PRO, PLAN_ENTERPRISE)

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey('tenant.id'),
        nullable=False,
        unique=True,   # 一个租户一份订阅
    )
    plan = db.Column(db.String(50), nullable=False, default=PLAN_FREE)
    quota_total = db.Column(db.Integer, default=100, nullable=False)
    quota_used = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now,
                           nullable=False)

    def __repr__(self):
        return f'<Subscription {self.id} tenant={self.tenant_id} {self.plan}>'


# --- 反向关系（Tenant 侧）---
Tenant.users = db.relationship(
    'User', back_populates='tenant', lazy='select',
)
Tenant.kbs = db.relationship(
    'Kb', back_populates='tenant', lazy='select',
)

# --- 需要租户隔离的表：scoped() 靠这张表决定加不加过滤 ---
TENANT_SCOPED_MODELS = (User, Kb, Document, Usage, Subscription)


def init_platform_admin(username, password_hash):
    """创建平台管理员（tenant_id 为 NULL，跨租户）。"""

    existing = User.query.filter_by(username=username).first()
    if existing is not None:
        return existing
    user = User(
        tenant_id=None,
        username=username,
        password_hash=password_hash,
        role=ROLE_PLATFORM_ADMIN,
    )
    db.session.add(user)
    db.session.commit()
    return user