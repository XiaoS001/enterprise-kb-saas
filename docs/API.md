# API 文档 · RMB01

Base URL：`http://127.0.0.1:5001`
认证：Cookie Session（`POST /api/auth/login` 后用同一 client 请求）

> 🔴 所有接口的跨租户访问**一律返回 404**，不是 403 ——
> 403 会泄漏「这个资源存在」。

---

## 一、健康检查

### `GET /healthz`

无需认证。只读配置快照，**不触发任何业务逻辑、不写库**。

```json
{
  "status": "ok",
  "service": "rmb01",
  "config": {
    "ai_key_present": false,
    "ai_enabled": true,
    "ai_model": "deepseek-chat",
    "app_port": 5001,
    "database_driver": "sqlite",
    "embedding_model": "BAAI/bge-small-zh-v1.5",
    "log_level": "INFO",
    "secret_key_ephemeral": true,
    "use_agent": false,
    "use_langchain": false,
    "vector_enabled": false
  }
}
```

🔴 **不返回密钥原文**，只返回 `ai_key_present` 这样的布尔值。

---

## 二、认证

### `POST /api/auth/register`

```json
{ "username": "alice", "password": "pass123456", "role": "org_admin", "tenant_id": 1 }
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `username` | ✅ | 长度有下限 |
| `password` | ✅ | 最短 8 位 |
| `role` | | 默认 `org_admin`；`platform_admin` 需单独初始化 |
| `tenant_id` | | `platform_admin` 传 null |

响应 `201`：

```json
{ "id": 1, "username": "alice", "role": "org_admin", "tenant_id": 1 }
```

错误 `400`：
- `用户名已存在`（不透露哪个字段错得更细，避免枚举）
- `密码长度不足`

### `POST /api/auth/login`

```json
{ "username": "alice", "password": "pass123456" }
```

响应 `200`：`{ "username": "...", "role": "...", "tenant_id": 1 }`

错误 `401`：`{"error": "用户名或密码错误"}` ——
🔴 **不区分「用户不存在」与「密码错误」**，避免用户枚举。

### `POST /api/auth/logout`

清 session，返回 `{"status": "ok"}`。

### `GET /api/auth/me`

需登录。返回当前用户与权限集：

```json
{
  "username": "alice",
  "role": "org_admin",
  "tenant_id": 1,
  "permissions": ["ask", "manage_org_users", "upload_doc", "view_history"]
}
```

---

## 三、知识库

### `GET /api/kbs`

需 `view` 权限。返回**本租户**的知识库列表。

```json
[{ "id": 1, "name": "运维知识库", "description": "...", "document_count": 3 }]
```

### `POST /api/kbs`

需 `create_kb` 权限。

```json
{ "name": "新库", "description": "可选" }
```

响应 `201`：`{ "id": 2, "name": "新库", ... }`

🔴 请求体里塞 `"tenant_id": 999` 会被**强制覆盖**（`tenant_fields()` 先 pop 再写入当前租户）。

### `GET /api/kbs/<kb_id>`

需 `view`。跨租户 → `404 {"error": "知识库不存在"}`。

### `DELETE /api/kbs/<kb_id>`

需 `delete_kb`。跨租户 → `404`，且**不会删掉别人的库**。

### `GET /api/kbs/<kb_id>/documents`

需 `view`。返回该库的文档分段（按 `chunk_index` 排序）。

```json
[{ "id": 1, "title": "运维手册", "chunk_index": 0, "content": "..." }]
```

### `POST /api/kbs/<kb_id>/documents`

需 `upload_doc`。

```json
{ "title": "运维手册", "content": "第一段。\n\n第二段。\n\n第三段。" }
```

响应 `201`：

```json
{ "chunks": [
  { "id": 1, "title": "运维手册", "chunk_index": 0, "content": "第一段。" },
  { "id": 2, "title": "运维手册", "chunk_index": 1, "content": "第二段。" },
  { "id": 3, "title": "运维手册", "chunk_index": 2, "content": "第三段。" }
]}
```

分段规则：**按空行切**（不是按单换行 —— 那会把段落内的句子拦腰截断）；
超长段落再按句子边界切，最后才硬切。

错误：
- `400` —— `title`/`content` 为空
- `404` —— 库不属于本租户（🔴 且**真的没写进去**）

### `DELETE /api/documents/<document_id>`

需 `upload_doc`。跨租户 → `404`。

---

## 四、问答（核心接口）

### `POST /api/kbs/<kb_id>/ask`

需 `ask` 权限。

```json
{ "question": "连接池大小怎么配", "top_k": 3, "use_agent": false }
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `question` | 必填 | 最长 500 字 |
| `top_k` | 3 | 召回段数 |
| `use_agent` | false | true 走 LangGraph 多智能体 |

响应 `200`：

```json
{
  "answer": "连接池大小建议按 CPU 核数乘以 2 再加磁盘数估算。",
  "references": [
    { "document_id": 2, "title": "运维手册", "chunk_index": 1,
      "score": 3.648, "preview": "连接池大小建议按..." }
  ],
  "citations": ["2"],
  "invalid_citations": [],
  "resolved_citations": [
    { "citation": "2", "document_id": 2, "title": "运维手册", "chunk_index": 1 }
  ],
  "degraded": false,
  "degradation_reasons": [],
  "retrieved_count": 1,
  "evidence": "[doc:2] 运维手册#1\n    连接池大小建议按...",
  "usage": {
    "billable": true,
    "degraded_no_charge": false,
    "quota": { "plan": "pro", "quota_used": 1, "quota_total": 10000,
               "quota_remaining": 9999, "exhausted": false }
  }
}
```

#### 🔴 三种特殊响应

**① 检索未命中**（库里没相关内容）：

```json
{ "answer": "知识库中没有相关内容。", "references": [],
  "retrieved_count": 0, "degraded": true,
  "degradation_reasons": ["检索未命中任何文档分段"] }
```

**不编造**，且**不扣额度**。

**② 降级**（无 API key / 模型失败）：`degraded: true` + `degradation_reasons`，
`usage.degraded_no_charge: true`，`quota.quota_used` **不变**。

**③ 额度用完** → HTTP `403`：

```json
{ "error": "本期额度已用完（100/100），可升级套餐",
  "code": "quota_exceeded", "plan": "free",
  "quota_used": 100, "quota_total": 100 }
```

🔴 **不是 500** —— 额度用完是正常业务状态。

错误码汇总：

| 状态码 | 场景 |
|---|---|
| `401` | 未登录 |
| `403` | 权限不足 / **额度用完** |
| `404` | 库不存在或不属于本租户 |
| `400` | 问题为空/超长 |

### `POST /api/ask`（全局问答，XM03 遗留）

不带租户隔离的旧接口，走 XM03 的固定题库。保留用于对照。

### `POST /api/agent/ask`

多智能体版本，同上。

---

## 五、企业后台

### `GET /api/org/billing`

需 `view_history`。

```json
{
  "tenant_id": 1,
  "quota": { "plan": "pro", "plan_name": "专业版",
             "quota_total": 10000, "quota_used": 350,
             "quota_remaining": 9650, "exhausted": false },
  "usage": { "period_days": 30, "total_queries": 400,
             "billable_queries": 350, "degraded_queries": 50,
             "total_tokens": 125000 },
  "plan_name": "专业版",
  "kb_count": 3,
  "member_count": 5
}
```

🔴 `billable_queries` 与 `degraded_queries` 分开 —— 降级高说明该换 key 或升配。

### `GET /api/org/members`

需 `manage_org_users`。成员/只读 → `403`。

```json
[{ "id": 1, "username": "alice", "role": "org_admin", "created_at": "2026-10-17" }]
```

🔴 只返回**本租户**成员。

### `PUT /api/org/members/<user_id>/role`

```json
{ "role": "viewer" }
```

| 情况 | 响应 |
|---|---|
| 本租户成员，改成功 | `200` |
| **别家成员** | 🔴 `404`（不是 403） |
| 改自己 | `400 不能修改自己的角色` |
| 角色名非法 | `400` |

---

## 六、运营后台

全部需 `cross_tenant` 权限（仅 `platform_admin`）。
企业管理员调用 → `403`。

### `GET /api/platform/tenants`

```json
[{ "id": 1, "name": "甲公司", "slug": "tenant-a", "plan": "pro",
   "quota_used": 350, "quota_total": 10000,
   "member_count": 5, "kb_count": 3 }]
```

### `GET /api/platform/usage-ranking?limit=10`

按已用额度降序：

```json
[{ "rank": 1, "tenant_id": 2, "tenant_name": "乙公司",
   "plan": "free", "quota_used": 900, "quota_total": 100 }]
```

### `PUT /api/platform/tenants/<tenant_id>/plan`

```json
{ "plan": "pro" }
```

| 情况 | 响应 |
|---|---|
| 升级 | `200`（已用量不变） |
| 🔴 **降级但已用量超新额度** | **`409`** + 说明 |
| 未知套餐 | `400` |
| 租户不存在 | `400` |

降级冲突的 `409` 正文：

```json
{ "error": "当前套餐已用 5000 次，超过免费版的 100 次额度，降级前需先处理已用量" }
```

**为什么是 409 而不是 400**：这是**业务冲突**（数据状态不允许），
不是参数错误。而且不能让系统"自动修好"——
把已用量截断成新额度等于凭空抹掉客户已消耗的额度。

### `GET /api/platform/plans`

```json
[{ "plan": "free", "name": "免费版", "quota_total": 100,
   "price_cny_month": 0, "features": ["5 个知识库", "..."] }]
```

---

## 七、通用错误格式

```json
{ "error": "人类可读的中文说明" }
```

额度相关额外带 `code`：

```json
{ "error": "...", "code": "quota_exceeded", "plan": "...",
  "quota_used": 0, "quota_total": 0 }
```

---

*（本文件随代码同步更新 · 2026-10-17）*