# RMB01 · 企业 AI 知识库问答 SaaS

> 多租户知识库问答平台：上传文档 → 分段建索引 → 带引用作答 → 计量计费。
> Flask + LangGraph + BM25，零前端构建步骤。

**这个项目解决什么问题**：企业内部文档散落在网盘、Wiki、群聊里，员工找答案靠问人。
把文档传进来，用自然语言问，答案**必须标出依据是哪份文档的第几段** ——
查不到就说查不到，不编。

---

## 一、快速开始

```bash
# 1. 装依赖
python -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt

# 2. 起服务（默认 5001 端口）
python app.py

# 3. 打开 http://127.0.0.1:5001
```

**没有 API key 也能跑** —— 问答会走降级路径（只返回检索证据，不生成答案），
且**不扣额度**。要完整效果就配 `DEEPSEEK_API_KEY`。

### Docker

```bash
docker compose up -d
# 或
docker build -t rmb01 . && docker run -p 5001:5001 -v rmb01-data:/app/data rmb01
```

### 演示数据

```bash
python demo_seed.py      # 建演示租户 + 三档账号 + 灌示例文档，并真跑一遍问答链路
python demo_citation.py  # 单独演示引用校验（含「故意引用不存在的文档」）
```

---

## 二、功能

| 模块 | 能力 |
|---|---|
| **多租户** | 6 张表全部带 `tenant_id`；双层隔离（显式 `scoped()` + ORM 自动注入）；跨租户一律 404 |
| **角色** | `platform_admin`（跨租户）/ `org_admin` / `member` / `viewer` 四档，权限矩阵集中定义 |
| **知识库** | 建库/改/删；上传 TXT·MD；按空行分段（两层级：分隔符 → 句子边界 → 硬切） |
| **问答** | BM25 检索（jieba 分词）→ 取证据 → 模型作答 → **引用校验** |
| **引用校验** | 答案里的 `[doc:N]` 必须指向真实存在的文档；有证据却零引用也算不合格 |
| **计量** | 每次问答记流水；**原子扣额度**（单语句 UPDATE + rowcount） |
| **套餐** | free / pro / enterprise 三档；降级时校验已用量，不产生负额度 |
| **后台** | 企业后台（本租户账单/成员）；运营后台（租户列表/用量排行/套餐变更） |

### 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| Web | Flask 3.1 + Flask-SQLAlchemy 3.1 | 无前端构建，`templates/` 直接渲染 |
| 多智能体 | LangGraph 1.2 | 规划 → 检索 → 评判 → 重写 → 作答 |
| 检索 | rank-bm25 + jieba | **零网络依赖**（向量检索默认关） |
| 数据库 | SQLite（WAL） | 单机够用；换 PostgreSQL 只需改 `RMB01_DATABASE_URL` |
| 部署 | gunicorn + Docker | 非 root 运行 |

---

## 三、目录结构

```
RMB01/
├── app.py              # 应用工厂 create_app() + 主路由
├── config.py           # 集中配置（含运行时函数 api_key()/use_agent()）
├── models.py           # 6 张表 + 角色 + 权限
├── tenancy.py          # 🔴 租户隔离核心（双层防线）
├── auth.py             # 密码哈希 + 认证 + 权限 + 认证路由
├── kb_api.py           # 知识库 CRUD + 上传 + 问答接口
├── kb_search.py        # 🔴 BM25 检索（停用词 + 相关性过滤）
├── chunking.py         # 分段（两层级策略）
├── billing.py          # 🔴 计量 + 原子扣额度 + 套餐
├── admin_api.py        # 企业后台 + 运营后台 API
├── pages.py            # 🔴 页面路由（4 个页面 + 服务端权限门控）
├── agent.py            # LangGraph 多智能体（源自 XM03）
├── skills/             # 可复用技能（引用校验/题目检索）
├── templates/          # base/login/kbs/org/platform/denied（无框架无 CDN）
├── tests/              # 287 条测试
├── docs/               # API / 数据库 / 运维 / 计费规则 / 商业模式 / 截图说明
│   └── screenshots/    # 4 张真实截图
├── run_mutations.py    # 🔴 17 个变异测试（CI 里真跑）
├── demo_seed.py        # 演示数据 + 真实链路输出
├── make_screenshots.py # 🔴 Playwright 自动截图 4 张
├── Dockerfile / docker-compose.yml / entrypoint.sh
└── .github/workflows/ci.yml
```

---

## 四、核心设计决策

### 4.1 🔴 租户隔离：两层防线，且能各自独立验证

```python
scoped(Kb, tenant_id)          # 第一层：显式过滤
Kb.query.all()                 # 第二层：ORM 层自动注入（with_loader_criteria）
```

**为什么两层**：任何一处漏写 `scoped()` 都不至于全库可见。

**但两层会互相掩护** —— 这带来一个隐蔽问题：如果测试都在租户上下文里跑，
打坏第一层时第二层会兜住，**测试全绿但第一层其实没有被验证过**
（实测过：变异①③④ 一度全部 0 红）。

所以补了一组「关掉另一道、单独验这一道」的测试：

```python
def test_scoped_alone_isolates_without_orm_context(...):
    reset_tenant_context()                     # 关掉第二道
    assert len(scoped(Kb, a.id).all()) == 1    # 第一道必须自己能隔离

def test_orm_layer_alone_isolates_without_scoped(...):
    set_tenant_context(a.id)                   # 关掉第一道
    assert len(Kb.query.all()) == 1            # 第二道也必须能
```

### 4.2 🔴 跨租户一律 404，不是 403

403 会泄漏「这个资源存在」——攻击者能靠状态码差异枚举出别家有多少知识库。

### 4.3 🔴 原子扣额度

```sql
UPDATE subscription SET quota_used = quota_used + 1
 WHERE id = :sid AND quota_used < quota_total     -- 判定必须在 SQL 里
```

看 `rowcount`：1 = 扣成功，0 = 额度不足。

**不能「先 SELECT 再 UPDATE」** —— 两个并发请求会同时读到 `quota_used=4`（总额 5），
都判定「还有额度」，都写回 6，**超扣**。

### 4.4 🔴 降级不扣额度

没配 API key、模型调用失败、检索未命中 —— 这些情况**没有生成答案**，
不该计费。这依赖 `agent` 的 `degraded` 标记说实话（它曾经硬编码 `False`，
等于收了钱没提供服务）。

### 4.5 运行时配置，不是 import 时常量

```python
def api_key() -> str:        # 而不是 API_KEY = os.environ.get(...)
    return _env('DEEPSEEK_API_KEY')
```

import 期固化的常量会让测试的 `monkeypatch.setenv` 失效
（实测让 9 条基线测试挂掉）。

### 4.6 🔴 引用格式与校验器必须对齐

答案里的引用写成 `[doc:<id>]`。校验器正则**必须认识这个格式**，
否则 `extract_citation_ids()` 返回 `[]` → `invalid_citations` 也是 `[]` →
`ok=True` —— **校验静默失效**，模型爱引用谁引用谁。

### 4.7 检索质量：停用词 + 相关性阈值

实测问题：「慢查询怎么排查」把《产品FAQ》的「额度**怎么**算」召回成第 2 名（2.78 分）。
「怎么」这种疑问词在小语料里仍是有效 term。

修法两层：**疑问词进停用词表** + **词面覆盖率要求（查询实词至少命中一半）**。

### 4.8 页面：4 个路由，不引框架与 CDN

| 路由 | 谁能进 | 作用 |
|---|---|---|
| `/login` | 所有人 | 登录 / 注册新租户 |
| `/kbs` | 已登录 | **核心页**：知识库列表 + 上传 + 问答 + 引用卡片 |
| `/org` | 仅企业管理员 | 账单、额度、成员管理 |
| `/platform` | 仅平台管理员 | 租户列表、用量排行、改套餐 |

🔴 **三条硬约束**（都是踩过坑才定的）：

1. **不引前端框架** —— 纯 Flask 模板 + 原生 `fetch`，无构建步骤
2. **不引 CDN** —— RMB03 踩过「CDN 拉不到 → 图表静默空白」的坑。
   `tests/test_pages.py::test_no_cdn_references_in_templates` 扫描模板里的外链
3. **权限在服务端挡，不只藏菜单** —— 成员手输 `/org` 也是 403 页面

菜单显示什么和服务端放行什么**必须是同一套规则**，否则会出现
「点得进去但 403」。`test_menu_rules_match_server_rules` 盯着这个。

截图见 [`docs/截图说明.md`](docs/截图说明.md)（**4 张真实截图**，由 `make_screenshots.py` 驱动 Playwright 拍摄，每张都断言了元素真实渲染）。

---

## 五、测试

```bash
pytest tests -q                      # 287 passed
pytest tests -q -m "not vector"      # CI 用法（向量测试会联网下载模型）
python run_mutations.py              # 17 个变异，CI 里真跑
```

| 类型 | 数量 | 说明 |
|---|---|---|
| XM03 基线 | 53 | 继承来的，**一条都没有退化** |
| 多租户 | 31 | 含「两道防线各自独立生效」 |
| 认证权限 | 29 | 密码哈希、四档角色、无用户枚举 |
| 知识库接口 | 20 | 每个接口配跨租户测试 |
| 引用校验 | 20 | 含格式对齐、零引用拦截、死循环防护 |
| 问答链路 | 13 | 上传→问→引用；降级不 500 |
| 分段与检索 | 12 | 含单文档库回归 |
| 检索质量 | 11 | 停用词、相关性过滤、跨库隔离 |
| 计费 | 20 | 原子扣减、账实相符、降级不扣 |
| 计费并发 | 10 | SQL 结构断言 + 串行边界 + 真并发 |
| 后台 | 29 | 跨租户 404、越权 403、降级 409 |
| 降级标记 | 7 | 含源码扫描防回归 |

**变异测试 17 个全部被抓**，包括：租户过滤失效、先查后减、`<` 改 `<=`、
不检查 rowcount、降级也扣额度、豁免变后门、降级截断已用量、后台去隔离。

> 🔴 runner 自带三层自检：注入成功 → 源码真的变了 → 语法能编译。
> 为什么需要：曾有一个变异只删了 `raise` 但 `if` 条件还在，逻辑反而更严，
> 「0 红」会被误读成「测试太弱」。**注入不完整的变异会给出假的安全感。**

---

## 六、配置

全部环境变量见 `.env.example`。关键几项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `RMB01_PORT` | 5001 | 监听端口 |
| `RMB01_DATABASE_URL` | `sqlite:///./data/rmb01.db` | 换 PostgreSQL 改这里 |
| `RMB01_VECTOR_ENABLED` | **false** | 向量检索；开了要联网下 90MB 模型 |
| `RMB01_BM25_RELATIVE_THRESHOLD` | 0.35 | 检索相关性阈值 |
| `DEEPSEEK_API_KEY` | 空 | 留空 → 降级路径，**不扣额度** |
| `RMB01_SECRET_KEY` | 随机 | 生产必配，否则重启后 session 全失效 |

⚠️ 未配 `RMB01_SECRET_KEY` 时启动会打警告并使用临时密钥
（`config.SECRET_KEY_IS_EPHEMERAL` 可查）。

---

## 七、部署约束

### SQLite → 单 worker

`docker-compose.yml` 里固定 `GUNICORN_WORKERS: "1"`。
**多 worker 会并发写冲突**（计量扣额度是写操作），`entrypoint.sh` 会拒绝启动并说明原因。

要横向扩展：换 PostgreSQL，然后设 `GUNICORN_WORKERS=N`。

### 已验证

| 项 | 怎么验的 |
|---|---|
| pytest 296 条 | 本地 + CI（GitHub Actions） |
| 变异 17 个 | CI 里 `python run_mutations.py` 真跑 |
| Docker 镜像构建 | CI `docker-build` job |
| 容器真跑起来 | CI `smoke-run`：`/healthz` 200、`id -un` = appuser、容器内能写库 |
| 非 root 运行 | CI smoke-run 断言 `appuser` |
| 多 worker 防呆 | CI smoke-run 断言 `GUNICORN_WORKERS=3` 时启动被拒 |
| 引用链路 | `demo_seed.py` 真实输出（见 `docs/演示脚本.md`） |
| 🔴 **真调模型一次** | 2026-10-18 · DeepSeek-V4-Flash @ AMD Radeon Cloud · `degraded=false`、答案由模型生成、`[doc:10]`/`[doc:11]` 指向《故障处理预案》。**仅单次跑通，未评测答案质量** |
| XM03 29 文件零改动 | 每阶段 sha256 比对 |

### ⚠️ 未验证 / 未实现

**这两栏是刻意分开的 —— 上面是「我真跑过」，下面是「我没跑过或没做」。**

| 项 | 状态 | 说明 |
|---|---|---|
| **真实模型作答效果** | ⚠️ **仅单次跑通** | 2026-10-18 用 DeepSeek-V4-Flash @ AMD Radeon Cloud 真调过一次：`degraded=false`、答案由模型生成、`[doc:10]`/`[doc:11]` 引用指向《故障处理预案》。**只证明链路通，答案质量未评测**（没有标注集、没有对比基线）。其余测试仍全部 mock 模型 |
| **向量检索** | ❌ 未启用 | 代码路径保留，`RMB01_VECTOR_ENABLED=false`。fastembed 装了但没跑过真实嵌入 |
| **真实并发压测** | ❌ 未做 | 只验证了「不超扣」，没测吞吐（没有 Locust/k6） |
| **`/metrics` 端点** | ❌ 未实现 | 所以 `docker-compose.yml` **没有** Prometheus/Grafana —— 摆着采不到业务指标是假把式 |
| **计费周期归零** | ❌ 未实现 | `quota_used` 只增不减，没有按月重置 |
| **真实支付** | ❌ 未接入 | 只有套餐与计量，无支付网关 |
| **邮件/短信通知** | ❌ 未实现 | 套餐表里写了但没做 |
| **前端** | ✅ 4 个页面 | 登录 / 知识库 / 企业后台 / 平台后台，见 4.8 节 |
| **演示截图** | ✅ 4 张 | 登录 / 知识库问答（含引用）/ 企业后台 / 平台后台，全部真实数据。复跑：`python make_screenshots.py` |
| **PostgreSQL** | ❌ 未验证 | 代码支持但只在 SQLite 上跑过 |
| **多语言** | ❌ 未支持 | 只有中文 |
| **数据迁移** | ❌ 未做 | 用 `db.create_all()`，没有 Alembic |

---

## 八、开发须知

### 加一个返回租户数据的接口

1. 用 `login_required('<permission>')` 装饰
2. **第一行**调 `scoped(Model, tenant_id)` 或 `fetch_scoped(Model, id)`
3. **同一个 PR 里**补两条测试：A 能拿到自己的、A 拿不到 B 的（断言 404）
4. 在 `run_mutations.py` 里加一个「去掉租户过滤」的变异，确认测试会红

**不要批量补测试** —— 加接口时就补，否则会攒成一堆没隔离的接口。

### 改动隔离相关代码后

```bash
pytest tests -q && python run_mutations.py
```

---

## 九、许可与说明

- 演示数据全部是**模拟数据**，不涉及任何真实企业资料
- 项目用于求职展示，非生产级产品（见第七节「未验证」清单）
- 继承自 `XM03` 的 29 个文件保持零改动，用于对照演进过程

---

*详见 [`docs/`](docs/)：API · 数据库设计 · 运维手册 · 计费规则 · 商业模式*
