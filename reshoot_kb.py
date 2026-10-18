"""只重拍第 2 张「知识库问答」。

## 为什么单独写一个脚本

原来的 `make_screenshots.py` 一次拍 4 张，重跑会连带覆盖另外 3 张。
这次只改第 2 张的取景，所以单独一个入口。

## 上一版错在哪

```python
page.evaluate('window.scrollTo(0, 0)')   # ← 滚到顶部
page.screenshot(...)                      # ← 问答区在下面被裁掉
```

1280x800 的视口里，页面从上到下是「导航 → 知识库头 → 上传区 → 文档列表 →
提问区 → 答案 → 引用卡片」。滚到顶部 + 800 高，截到的只有前四块，
**引用卡片根本不在图里** —— 而这张图的意义就是展示引用。

## 这一版怎么取景

1. 先隐藏「上传文档」块（用户已授权这个做法）
2. 视口 1280x900（不再调高，上次调高导致截图超时）
3. 提一个有答案的问题，等答案 + 引用渲染
4. **量元素的实际位置**：答案正文、引用卡片、降级徽标三者必须都落在
   视口内（bottom < innerHeight 且 top >= 0），否则滚动到刚好能装下
5. 截图后打印三个元素的位置，人工复核有据可查
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / 'docs' / 'screenshots'
BASE = 'http://127.0.0.1:5099'
TARGET = OUT / '2-知识库问答.png'

# 复用主脚本里的账号与文档，避免两处维护
from make_screenshots import ACCOUNTS, seed_via_api  # noqa: E402


def main():
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        print('❌ playwright 未安装（本脚本假定已装好）')
        return 1

    import requests

    OUT.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    ids = seed_via_api(session)
    print(f'演示数据就绪：租户 {ids["tenant_id"]} · 知识库 {ids["kb_id"]}')

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # 🔴 900 高，不再调高（上次调高触发截图超时）
        ctx = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = ctx.new_page()

        page.goto(f'{BASE}/login')
        page.get_by_role('textbox', name='用户名', exact=True).fill(ACCOUNTS['admin'][0])
        page.get_by_label('密码', exact=True).fill(ACCOUNTS['admin'][1])
        page.get_by_role('button', name='登录', exact=True).click()
        expect(page).to_have_url(f'{BASE}/kbs')

        # 选知识库
        page.locator('#kb-list > div').first.click()
        expect(page.get_by_text('故障处理预案')).to_be_visible()

        # ① 隐藏上传区（取景需要，用户已授权）
        page.evaluate(
            "document.getElementById('upload-card').classList.add('hidden')")

        # ② 提一个**知识库里确实有答案**的问题
        #    《故障处理预案》第 0 段就是「数据库连接超时怎么处理？」
        page.locator('#question').fill('数据库连接超时怎么处理')
        page.get_by_role('button', name='提问', exact=True).click()

        # ③ 等答案 + 引用都渲染出来
        expect(page.locator('#answer-box')).to_be_visible(timeout=20000)
        expect(page.locator('#refs .ref').first).to_be_visible(timeout=20000)
        page.wait_for_timeout(600)      # 等字体/布局稳定

        answer_text = page.locator('#answer').inner_text()
        ref_text = page.locator('#refs .ref .src').first.inner_text()
        badge_text = page.locator('#degraded-badge').inner_text()
        ref_count = page.locator('#refs .ref').count()

        print(f'  答案正文：{answer_text[:50]}')
        print(f'  引用卡片：{ref_text}')
        print(f'  状态徽标：{badge_text or "(无)"}')
        print(f'  引用条数：{ref_count}')

        # ④ 🔴 量三个元素的实际位置，确保都在视口内
        def boxes():
            return page.evaluate("""() => {
                const vh = window.innerHeight;
                const pick = (sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                            visible: r.top >= 0 && r.bottom <= vh};
                };
                return {
                    viewportH: vh,
                    scrollY: Math.round(window.scrollY),
                    docH: Math.round(document.documentElement.scrollHeight),
                    answer: pick('#answer'),
                    ref: pick('#refs .ref'),
                    badge: pick('#degraded-badge'),
                };
            }""")

        info = boxes()
        targets = ['answer', 'ref', 'badge']
        need_scroll = any(
            info[k] is None or not info[k]['visible'] for k in targets
        )

        if need_scroll:
            # 滚到「刚好能让引用卡片底部露出一小节」的位置：
            # 让 #answer-box 的底边落在视口底部上方 40px
            page.evaluate("""() => {
                const box = document.getElementById('answer-box');
                const r = box.getBoundingClientRect();
                const target = window.scrollY + r.bottom - window.innerHeight + 40;
                window.scrollTo(0, Math.max(0, target));
            }""")
            page.wait_for_timeout(500)
            info = boxes()
            print('  （已滚动到问答区）')

        print(f'  视口高 {info["viewportH"]} · 文档高 {info["docH"]} · '
              f'scrollY {info["scrollY"]}')
        for k in targets:
            b = info[k]
            state = '视口内' if b and b['visible'] else '🔴 不可见'
            print(f'    {k:<7} top={b["top"] if b else "-":>5} '
                  f'bottom={b["bottom"] if b else "-":>5}  {state}')

        missing = [k for k in targets
                   if info[k] is None or not info[k]['visible']]
        if missing:
            print(f'❌ 这些元素没进视口，拒绝截图：{missing}')
            browser.close()
            return 1

        page.screenshot(path=TARGET, full_page=False)
        browser.close()

    size = TARGET.stat().st_size
    print(f'\n✅ 已重拍：{TARGET.name}  {size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())