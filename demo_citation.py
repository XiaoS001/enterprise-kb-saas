"""引用链路实测 —— 打印真实运行结果，供人工核对。

用法：
    python demo_citation.py

这个脚本做三件事（对应复验指示第五节的报告要求）：
  1. 上传 → 问 → 答 + 引用 → 解析出的文档与段落
  2. 故意让答案引用不存在的 doc_id → 看校验器返 ok=False
  3. 问库里没有的问题 → 看是否如实说「没有相关内容」
"""
import os
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

os.environ.setdefault('RMB01_SCHEDULER_ENABLED', 'false')
os.environ.pop('DEEPSEEK_API_KEY', None)     # 无 key 也能跑（走降级）

from tenant_fixtures import make_app          # noqa: E402

from auth import register_user                # noqa: E402
from models import Kb, Tenant, db             # noqa: E402
from skills.check_citation import check_citation  # noqa: E402


HANDBOOK = """数据库连接池配置指南。

连接池的大小应该根据 CPU 核数乘以 2 再加磁盘数来估算。

慢查询排查步骤：先看 EXPLAIN 输出，再看索引命中率。

缓存穿透的常见解法是布隆过滤器和空值缓存。"""


def main():
    app = make_app()
    with app.app_context():
        tenant = Tenant(name='甲公司', slug='demo-tenant')
        db.session.add(tenant)
        db.session.commit()
        kb = Kb(tenant_id=tenant.id, name='运维手册')
        db.session.add(kb)
        db.session.commit()
        register_user('demo-admin', 'pass123456', 'org_admin', tenant.id)

        client = app.test_client()
        client.post('/api/auth/login',
                    json={'username': 'demo-admin', 'password': 'pass123456'})

        print('=' * 74)
        print('① 引用链路实测：上传 → 问 → 答 + 引用 → 解析回文档')
        print('=' * 74)

        upload = client.post(f'/api/kbs/{kb.id}/documents', json={
            'title': '运维手册', 'content': HANDBOOK,
        })
        chunks = upload.get_json()['chunks']
        print(f'上传：HTTP {upload.status_code} · 切成 {len(chunks)} 段')
        for c in chunks:
            print(f"    id={c['id']} #{c['chunk_index']} {c['content'][:32]}")

        ask = client.post(f'/api/kbs/{kb.id}/ask', json={
            'question': '连接池大小怎么估算',
        })
        body = ask.get_json()
        print(f"\n提问：连接池大小怎么估算  → HTTP {ask.status_code}")
        print(f"检索命中：{body['retrieved_count']} 段")
        print('引用（references）：')
        for ref in body['references']:
            print(f"    doc={ref['document_id']} 《{ref['title']}》第 {ref['chunk_index']} 段")
            print(f"        {ref['preview'][:50]}")
        print(f"degraded={body['degraded']}  原因={body['degradation_reasons']}")

        print()
        print('=' * 74)
        print('② 引用校验：故意引用一个不存在的 doc_id')
        print('=' * 74)
        real_ids = [str(r['document_id']) for r in body['references']]

        fake = check_citation('依据 [doc:99999] 的内容。', [{'id': i} for i in real_ids])
        print(f'答案引用 [doc:99999]，实际证据 id={real_ids}')
        print(f"  ok                = {fake['ok']}")
        print(f"  citations         = {fake['citations']}")
        print(f"  invalid_citations = {fake['invalid_citations']}")
        print(f"  → 🔴 引用校验{'拦住了编造' if not fake['ok'] else '没拦住（有问题！）'}")

        good = check_citation(f"依据 [doc:{real_ids[0]}] 的内容。",
                              [{'id': i} for i in real_ids])
        print(f"\n答案引用真实存在的 [doc:{real_ids[0]}]：")
        print(f"  ok = {good['ok']}  citations = {good['citations']}")

        no_cite = check_citation('答案完全没有引用任何东西。',
                                [{'id': i} for i in real_ids])
        print('\n有证据但答案零引用：')
        print(f"  ok     = {no_cite['ok']}")
        print(f"  reason = {no_cite.get('reason')}")

        print()
        print('=' * 74)
        print('③ 问库里没有的问题 → 是否如实说「没有相关内容」')
        print('=' * 74)
        miss = client.post(f'/api/kbs/{kb.id}/ask', json={
            'question': '公司年会在哪里办',
        })
        mbody = miss.get_json()
        print(f'提问：公司在哪里办 → HTTP {miss.status_code}')
        print(f"  answer          = {mbody['answer']}")
        print(f"  retrieved_count = {mbody['retrieved_count']}")
        print(f"  references      = {mbody['references']}")
        print(f"  → {'✅ 如实说没有，不编造' if not mbody['references'] else '❌ 编造了引用'}")

    print()
    print('=' * 74)
    print('说明：本脚本使用本地内存库，数据不落盘；无 API key，全程走降级路径。')
    print('=' * 74)
    return 0


if __name__ == '__main__':
    sys.exit(main())