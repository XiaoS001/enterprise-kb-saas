"""演示数据 + 真实问答链路输出。

⚠️ 本脚本生成的是**模拟数据**，问答对象是自己建的知识库，
   不涉及任何真实企业的资料。

用法：
    python demo_seed.py

做三件事：
  1. 建演示租户 + 三档角色账号
  2. 灌一份运维/产品知识文档并分段
  3. 真跑一次问答，打印完整链路（检索 → 引用 → 计量）
"""
import os
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

# 演示用独立库，不碰 data/rmb01.db
_demo_dir = pathlib.Path(tempfile.mkdtemp(prefix='rmb01-demo-'))
os.environ['RMB01_DATA_DIR'] = str(_demo_dir)
os.environ.setdefault('RMB01_VECTOR_ENABLED', 'false')
os.environ.pop('DEEPSEEK_API_KEY', None)      # 无 key 也能演示（走降级）

from tenant_fixtures import make_app          # noqa: E402

from auth import register_user                # noqa: E402
from billing import PLANS, ensure_subscription, quota_status, usage_summary  # noqa: E402
from models import (                          # noqa: E402
    ROLE_MEMBER,
    ROLE_ORG_ADMIN,
    ROLE_PLATFORM_ADMIN,
    ROLE_VIEWER,
    Kb,
    Tenant,
    db,
)


DOCS = [
    ('运维手册', """数据库连接池配置指南。

连接池大小建议按 CPU 核数乘以 2 再加磁盘数估算，超过这个数值反而会拖慢查询。

慢查询排查按以下顺序：先看 EXPLAIN 输出确认索引是否命中，再看是否发生全表扫描，最后才考虑加索引。

缓存穿透的标准解法有两种：布隆过滤器拦截不存在的 key，或对空结果做短期缓存。"""),

    ('产品FAQ', """如何新建知识库？

进入「知识库」页面，点「新建」，填名称即可。企业管理员和成员都能建库，只读账号不能。

支持哪些文档格式？

当前支持 TXT 与 Markdown。上传后系统会按空行分段，每段独立建索引。

额度怎么算？

每次问答扣 1 次。如果服务降级（比如没配模型密钥），不扣额度。"""),

    ('安全说明', """数据隔离是怎么做的？

所有查询都经 scoped() 强制带租户过滤，跨租户一律返回 404 而不是 403。

密码怎么存的？

PBKDF2-SHA256，12 万轮，每个用户一个随机盐，比较用常量时间。"""),
]


def main():
    print('=' * 72)
    print('⚠️  本脚本生成的是模拟数据，不涉及任何真实企业的资料')
    print('=' * 72)
    print(f'演示库（临时目录，不落盘到 data/）：{_demo_dir}')
    print()

    app = make_app(database_uri=f'sqlite:///{(_demo_dir / "rmb01.db").as_posix()}')
    with app.app_context():
        db.create_all()

        # --- 1. 租户与账号 ---
        demo = Tenant(name='演示公司', slug='demo')
        other = Tenant(name='另一家公司', slug='other')
        db.session.add_all([demo, other])
        db.session.commit()
        ensure_subscription(demo.id, 'pro')
        ensure_subscription(other.id, 'free')

        accounts = []
        for username, role, pwd, label in [
            ('demo-admin', ROLE_ORG_ADMIN, 'demo123456', '企业管理员'),
            ('demo-member', ROLE_MEMBER, 'demo123456', '成员'),
            ('demo-viewer', ROLE_VIEWER, 'demo123456', '只读'),
        ]:
            u, err = register_user(username, pwd, role, demo.id)
            assert err is None, err
            accounts.append((username, pwd, label))
        platform, _ = register_user('demo-platform', 'demo123456', ROLE_PLATFORM_ADMIN)
        accounts.append(('demo-platform', 'demo123456', '平台管理员（跨租户）'))

        print('演示账号：')
        for username, pwd, label in accounts:
            print(f'  {username:<16} / {pwd:<12} {label}')
        print()

        # --- 2. 建库 + 灌文档 ---
        client = app.test_client()
        client.post('/api/auth/login',
                    json={'username': 'demo-admin', 'password': 'demo123456'})

        kb_resp = client.post('/api/kbs', json={
            'name': '运维知识库',
            'description': '演示用的运维与产品文档',
        })
        kb_id = kb_resp.get_json()['id']
        print(f'建库：HTTP {kb_resp.status_code} · kb_id={kb_id}')

        for title, content in DOCS:
            r = client.post(f'/api/kbs/{kb_id}/documents',
                            json={'title': title, 'content': content})
            n = len(r.get_json().get('chunks', []))
            print(f'  上传《{title}》→ HTTP {r.status_code} · {n} 段')

        print()
        print('=' * 72)
        print('真实问答链路实测（无 API key，走降级路径）')
        print('=' * 72)

        for question in ['连接池大小怎么配', '慢查询怎么排查', '额度怎么算', '公司年会在哪办']:
            r = client.post(f'/api/kbs/{kb_id}/ask', json={'question': question})
            body = r.get_json()
            print(f'\n问：{question}')
            print(f'  HTTP {r.status_code} · 命中 {body["retrieved_count"]} 段 · '
                  f'降级={body["degraded"]}')
            for ref in body['references'][:2]:
                print(f'    引用 → doc={ref["document_id"]} '
                      f'《{ref["title"]}》第 {ref["chunk_index"]} 段')
                print(f'           {ref["preview"][:52]}')
            if not body['references']:
                print(f'    答案：{body["answer"]}')

        print()
        print('=' * 72)
        print('计量状态')
        print('=' * 72)
        q = quota_status(demo.id)
        u = usage_summary(demo.id)
        print(f'  套餐：{q["plan_name"]} · {q["quota_used"]}/{q["quota_total"]}')
        print(f'  问答总数 {u["total_queries"]} · 计费 {u["billable_queries"]} · '
              f'降级 {u["degraded_queries"]}')
        print(f'  → 降级的不扣额度，所以 quota_used={q["quota_used"]}')

        print()
        print('=' * 72)
        print('跨租户隔离实测')
        print('=' * 72)
        client.post('/api/auth/logout')
        client.post('/api/auth/login',
                    json={'username': 'demo-viewer', 'password': 'demo123456'})
        r1 = client.get(f'/api/kbs/{kb_id}')
        print(f'  viewer 查自己的库   → HTTP {r1.status_code}')
        r2 = client.get('/api/kbs')
        print(f'  viewer 列库         → HTTP {r2.status_code} · {len(r2.get_json())} 个')
        client.post('/api/auth/logout')
        client.post('/api/auth/login',
                    json={'username': 'demo-admin', 'password': 'demo123456'})
        r3 = client.post(f'/api/kbs/{kb_id}/ask', json={'question': 'x'})
        print(f'  admin 问答自己的库  → HTTP {r3.status_code}')

    print()
    print('=' * 72)
    print('说明：本脚本用临时目录的内存/文件库，不写入 data/rmb01.db。')
    print('=' * 72)
    return 0


if __name__ == '__main__':
    sys.exit(main())