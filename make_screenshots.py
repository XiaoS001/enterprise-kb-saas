"""生成演示截图（4 张）。

用法：
    python make_screenshots.py

前提：
    pip install playwright && python -m playwright install chromium

## 为什么用 Playwright 而不是 Tabbit

Tabbit 是全局常驻实例，连续拍照时每张都超时（`fonts loaded` 之后卡住）。
本地 Playwright 每次起一个干净浏览器，没有状态残留。

## 🔴 截图前必须做的事

1. 用**独立临时库**，绝不碰 `data/rmb01.db`
2. 灌好演示数据（租户 / 三个账号 / 一个知识库 / 四份文档）
3. 每张图都断言页面元素真的渲染出来了 —— 不能拍一张空白还当成成功
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / 'docs' / 'screenshots'
BASE = 'http://127.0.0.1:5099'

ACCOUNTS = {
    'admin': ('demo-admin', 'demo123456'),
    'member': ('demo-member', 'demo123456'),
    'platform': ('demo-platform', 'demo123456'),
}

DOCS = [
    ('运维手册',
     '数据库连接池配置指南。\n\n'
     '连接池大小建议按 CPU 核数乘以 2 再加磁盘数估算，超过这个数值反而会拖慢查询。\n\n'
     '慢查询排查按以下顺序：先看 EXPLAIN 输出确认索引是否命中，'
     '再看是否发生全表扫描，最后才考虑加索引。\n\n'
     '缓存穿透的标准解法有两种：布隆过滤器拦截不存在的 key，或对空结果做短期缓存。'),
    ('产品FAQ',
     '如何新建知识库？\n\n'
     '进入知识库页面，点新建，填名称即可。企业管理员和成员都能建库，只读账号不能。\n\n'
     '额度怎么算？\n\n'
     '每次问答扣 1 次。如果服务降级（比如没配模型密钥），不扣额度。'),
    ('安全说明',
     '数据隔离是怎么做的？\n\n'
     '所有查询都强制带租户过滤，跨租户一律返回 404 而不是 403。\n\n'
     '密码怎么存的？\n\n'
     'PBKDF2-SHA256，12 万轮，每个用户一个随机盐。'),
    ('故障处理预案',
     '数据库连接超时怎么处理？\n\n'
     '先确认连接池是否打满，再检查慢查询日志。如果连接数接近上限，'
     '重启应用进程可以临时缓解，但必须同步排查慢查询根因。\n\n'
     '磁盘写满怎么办？\n\n'
     '先清理日志目录，再检查是否有大文件未轮转。紧急情况下可以扩容磁盘，'
     '但长期要配置日志轮转策略。'),
]


def seed_via_api(session):
    """用 HTTP 接口灌数据 —— 走真实链路，不是直接写库。"""

    r = session.post(f'{BASE}/api/tenants', json={'name': '演示公司'})
    r.raise_for_status()
    tenant_id = r.json()['id']

    for key, role in (('admin', 'org_admin'), ('member', 'member')):
        username, password = ACCOUNTS[key]
        session.post(f'{BASE}/api/auth/register', json={
            'username': username, 'password': password,
            'role': role, 'tenant_id': tenant_id,
        })

    username, password = ACCOUNTS['platform']
    session.post(f'{BASE}/api/auth/register', json={
        'username': username, 'password': password, 'role': 'platform_admin',
    })

    session.post(f'{BASE}/api/auth/login', json={
        'username': ACCOUNTS['admin'][0], 'password': ACCOUNTS['admin'][1],
    })

    r = session.post(f'{BASE}/api/kbs', json={'name': '运维知识库'})
    r.raise_for_status()
    kb_id = r.json()['id']

    for title, content in DOCS:
        session.post(f'{BASE}/api/kbs/{kb_id}/documents',
                     json={'title': title, 'content': content})

    return {'tenant_id': tenant_id, 'kb_id': kb_id}


def main():
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        print('❌ 未安装 playwright：pip install playwright')
        print('   然后：python -m playwright install chromium')
        return 1

    import requests

    OUT.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    ids = seed_via_api(session)
    print(f'演示数据就绪：租户 {ids["tenant_id"]} · 知识库 {ids["kb_id"]}')

    shots = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ---------- 1. 登录页 ----------
        ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = ctx.new_page()
        page.goto(f'{BASE}/login')
        expect(page.get_by_role('heading', name='登录')).to_be_visible()
        page.screenshot(path=OUT / '1-登录页.png')
        shots.append('1-登录页.png')
        print('  ✅ 1 登录页')

        # ---------- 2. 知识库问答（核心） ----------
        page.get_by_role('textbox', name='用户名', exact=True).fill(ACCOUNTS['admin'][0])
        page.get_by_label('密码', exact=True).fill(ACCOUNTS['admin'][1])
        page.get_by_role('button', name='登录', exact=True).click()
        expect(page).to_have_url(f'{BASE}/kbs')

        # 选第一个知识库
        page.locator('#kb-list > div').first.click()
        expect(page.get_by_text('运维手册')).to_be_visible()

        # 收起上传区，让「文档列表 + 提问 + 引用」同屏
        page.evaluate(
            "document.getElementById('upload-card').classList.add('hidden')")
        page.locator('#question').fill('数据库连接超时怎么处理')
        page.get_by_role('button', name='提问', exact=True).click()

        # 🔴 断言引用卡片真的出来了 —— 不能拍一张空白的当成功
        expect(page.locator('#refs .ref').first).to_be_visible(timeout=15000)
        ref_text = page.locator('#refs .ref .src').first.inner_text()
        assert '依据《' in ref_text, f'引用卡片文案不对：{ref_text}'
        assert '段' in ref_text, f'引用没标到第几段：{ref_text}'
        print(f'     引用卡片：{ref_text}')

        # 🔴 取景：视口要够高把「答案 + 引用卡片」一起装进来。
        #
        #    之前的写法是 scrollTo(0,0) + 800 高 —— 滚到顶部后
        #    问答区掉在视口下方被裁掉，截出来的图里**没有引用卡片**，
        #    而这张图的意义就是展示引用。
        #    900 高时整页正好装下（实测 docH = viewportH = 900）。
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.wait_for_timeout(500)

        # 量三个关键元素是否真的在视口内，不在就拒绝出图
        geo = page.evaluate("""() => {
            const vh = window.innerHeight;
            const pick = (sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                        visible: r.top >= 0 && r.bottom <= vh};
            };
            return {vh, answer: pick('#answer'), ref: pick('#refs .ref'),
                    badge: pick('#degraded-badge')};
        }""")
        hidden = [k for k in ('answer', 'ref', 'badge')
                  if geo[k] is None or not geo[k]['visible']]
        assert not hidden, f'🔴 取景失败，这些元素不在视口内：{hidden}'
        print(f'     取景 OK（视口 {geo["vh"]}）：'
              f'answer top={geo["answer"]["top"]} · '
              f'ref top={geo["ref"]["top"]}')

        page.screenshot(path=OUT / '2-知识库问答.png')
        shots.append('2-知识库问答.png')
        print('  ✅ 2 知识库问答')
        ctx.close()

        # ---------- 3. 企业后台 ----------
        ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = ctx.new_page()
        page.goto(f'{BASE}/login')
        page.get_by_role('textbox', name='用户名', exact=True).fill(ACCOUNTS['admin'][0])
        page.get_by_label('密码', exact=True).fill(ACCOUNTS['admin'][1])
        page.get_by_role('button', name='登录', exact=True).click()
        expect(page).to_have_url(f'{BASE}/kbs')
        page.goto(f'{BASE}/org')

        # 断言统计数字渲染出来了（不是 "加载中…"）
        expect(page.locator('#stats .num').first).to_be_visible()
        stat_count = page.locator('#stats .num').count()
        assert stat_count >= 6, f'企业后台统计项应 >= 6，实际 {stat_count}'
        quota_text = page.locator('#quota-label').inner_text()
        assert '额度' not in quota_text or '/' in quota_text
        print(f'     额度：{quota_text}')

        page.evaluate('window.scrollTo(0, 0)')
        page.wait_for_timeout(400)
        page.screenshot(path=OUT / '3-企业后台.png')
        shots.append('3-企业后台.png')
        print('  ✅ 3 企业后台')
        ctx.close()

        # ---------- 4. 平台后台 ----------
        ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = ctx.new_page()
        page.goto(f'{BASE}/login')
        page.get_by_role('textbox', name='用户名', exact=True).fill(ACCOUNTS['platform'][0])
        page.get_by_label('密码', exact=True).fill(ACCOUNTS['platform'][1])
        page.get_by_role('button', name='登录', exact=True).click()
        expect(page).to_have_url(f'{BASE}/kbs')
        page.goto(f'{BASE}/platform')

        expect(page.locator('#tenants table')).to_be_visible(timeout=15000)
        rows = page.locator('#tenants tbody tr').count()
        assert rows >= 1, f'租户列表应有数据，实际 {rows} 行'
        rank_rows = page.locator('#ranking tbody tr').count()
        print(f'     租户 {rows} 个 · 排行 {rank_rows} 条')

        page.evaluate('window.scrollTo(0, 0)')
        page.wait_for_timeout(400)
        page.screenshot(path=OUT / '4-平台后台.png')
        shots.append('4-平台后台.png')
        print('  ✅ 4 平台后台')
        ctx.close()

        browser.close()

    print()
    for name in shots:
        path = OUT / name
        print(f'  {name:<24} {path.stat().st_size:>7} bytes')
    print(f'\n共 {len(shots)} 张 -> {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())