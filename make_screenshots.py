"""生成演示截图（4 张）。

用法：
    python make_screenshots.py                      # 默认拍 127.0.0.1:5001
    RMB01_SHOT_BASE=http://127.0.0.1:5099 python make_screenshots.py

前提：
    pip install playwright && python -m playwright install chromium
    对面得是**已经有演示数据**的站点（demo_seed.py 或仓库自带的演示库）

## 为什么用 Playwright 而不是 Tabbit

Tabbit 是全局常驻实例，连续拍照时每张都超时（`fonts loaded` 之后卡住）。
本地 Playwright 每次起一个干净浏览器，没有状态残留。

## 🔴 脚本不造数据，只拍数据

1. 数据来自 `demo_seed.py`（临时库）或仓库自带的 `data/rmb01.db`
2. 拍之前先探一遍：登得上、有库、成员表 3 种角色 —— 缺什么直接报错
3. 每张图都断言页面元素真的渲染出来了 —— 不能拍一张空白还当成成功
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / 'docs' / 'screenshots'
BASE = (os.environ.get('RMB01_SHOT_BASE') or 'http://127.0.0.1:5001').rstrip('/')

ACCOUNTS = {
    'admin': ('demo-admin', 'demo123456'),
    'member': ('demo-member', 'demo123456'),
    'viewer': ('demo-viewer', 'demo123456'),
    'platform': ('demo-platform', 'demo123456'),
}



def check_demo_site(session):
    """拍之前先探一遍对面 —— 只读，不写库。

    缺数据就当场报清楚缺什么，别拍到一半才发现空表。
    """

    r = session.get(f'{BASE}/healthz', timeout=5)
    r.raise_for_status()

    r = session.post(f'{BASE}/api/auth/login', json={
        'username': ACCOUNTS['admin'][0], 'password': ACCOUNTS['admin'][1],
    })
    if r.status_code != 200:
        raise SystemExit(
            f'❌ {BASE} 登不上 {ACCOUNTS["admin"][0]} —— '
            '先跑 python demo_seed.py，或确认对面是演示站点')

    billing = session.get(f'{BASE}/api/org/billing').json()
    kbs = session.get(f'{BASE}/api/kbs').json()
    if not kbs:
        raise SystemExit('❌ 演示站点没有知识库 —— 先跑 python demo_seed.py')

    members = session.get(f'{BASE}/api/org/members').json()
    if len(members) < 3:
        raise SystemExit(
            f'❌ 成员只有 {len(members)} 个；企业后台那张图要三种角色同屏 —— '
            '先跑 python demo_seed.py')

    session.post(f'{BASE}/api/auth/logout')

    r = session.post(f'{BASE}/api/auth/login', json={
        'username': ACCOUNTS['platform'][0], 'password': ACCOUNTS['platform'][1],
    })
    if r.status_code != 200:
        raise SystemExit(
            f'❌ 登不上 {ACCOUNTS["platform"][0]}；平台后台那张图需要平台账号 —— '
            '先跑 python demo_seed.py')
    session.post(f'{BASE}/api/auth/logout')

    print(f'     站点 {BASE}：库 {kbs[0]["id"]}「{kbs[0]["name"]}」· '
          f'成员 {len(members)} 个 · 已用额度 '
          f'{(billing.get("quota") or {}).get("quota_used", "?")}')
    return {'tenant_id': billing.get('tenant_id'), 'kb_id': kbs[0]['id']}


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
    ids = check_demo_site(session)

    shots = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ---------- 1. 登录页 ----------
        ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = ctx.new_page()
        page.goto(f'{BASE}/login')
        expect(page.locator('#login-username')).to_be_visible()
        expect(page.locator('#demo-grid .demo-card').first).to_be_visible()
        page.screenshot(path=OUT / '1-登录页.png')
        shots.append('1-登录页.png')
        print('  ✅ 1 登录页')

        # ---------- 2. 知识库问答（核心） ----------
        page.fill('#login-username', ACCOUNTS['admin'][0])
        page.fill('#login-password', ACCOUNTS['admin'][1])
        page.click('#login-submit')
        expect(page).to_have_url(f'{BASE}/kbs')

        # 列表页 → 第一个知识库（2026-10-21 起问答是独立页面）
        page.locator('.kb-item .name a').first.click()
        expect(page).to_have_url(re.compile(r'/kbs/\d+$'))
        kb_id = page.url.rstrip('/').rsplit('/', 1)[-1]

        page.goto(f'{BASE}/kbs/{kb_id}/ask', wait_until='networkidle')
        expect(page.locator('#question')).to_be_visible()
        page.locator('#question').fill('数据库连接超时怎么处理')
        page.click('#ask-btn')

        # 🔴 断言引用卡片真的出来了 —— 不能拍一张空白的当成功
        expect(page.locator('#refs .ref-card').first).to_be_visible(timeout=15000)
        ref_text = page.locator('#refs .ref-card .src').first.inner_text()
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
            return {vh, answer: pick('#result'), ref: pick('#refs .ref-card')};
        }""")
        hidden = [k for k in ('answer', 'ref')
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
        page.fill('#login-username', ACCOUNTS['admin'][0])
        page.fill('#login-password', ACCOUNTS['admin'][1])
        page.click('#login-submit')
        expect(page).to_have_url(f'{BASE}/kbs')
        page.goto(f'{BASE}/org', wait_until='networkidle')

        # 断言统计数字渲染出来了（不是 "加载中…"）
        expect(page.locator('#kpis .kpi').first).to_be_visible()
        stat_count = page.locator('#kpis .kpi').count()
        assert stat_count >= 4, f'企业后台统计项应 >= 4，实际 {stat_count}'
        first_kpi = ' '.join(page.locator('#kpis .kpi').first.inner_text().split())
        print(f'     首个 KPI：{first_kpi}')
        # 成员表必须真的渲染出来（这一屏的意义就是「能管人」）
        members = page.locator('.members-table tbody tr').count()
        assert members >= 3, f'成员表应 >= 3 行，实际 {members}'

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
        page.fill('#login-username', ACCOUNTS['platform'][0])
        page.fill('#login-password', ACCOUNTS['platform'][1])
        page.click('#login-submit')
        # 平台账号没有租户，登录后落在平台后台（不是 /kbs）
        expect(page).to_have_url(f'{BASE}/platform')

        expect(page.locator('#tenants table')).to_be_visible(timeout=15000)
        rows = page.locator('#tenants tbody tr').count()
        assert rows >= 1, f'租户列表应有数据，实际 {rows} 行'
        kpis = page.locator('#kpis .kpi .num').count()
        print(f'     租户 {rows} 个 · KPI {kpis} 项')

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