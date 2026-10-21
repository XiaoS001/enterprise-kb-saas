"""变异测试运行器 —— 租户隔离 / 引用校验 / 计费原子性。

用法：
    python run_mutations.py

三种变异注入方式：
  · replace       —— 字符串替换（适合单行）
  · replace_block —— 多行整块替换（适合函数体）
  · replace_lines —— 按锚点行定位，替换到 until 行为止
                     整块字符串匹配对缩进/注释太敏感，源码一改就注入失败；
                     这个方式稳得多，变异 9 最初就是因为块匹配失败而「注入无效」

设计要点：
  · 🔴 全部变异都在**临时工作区**（项目副本）里做，真实源码不动
  · 每个变异跑完立刻还原副本里的文件，不等下一轮开头清理
  · 注入后自检「文件里真的有 MUTANT」，否则「0 条红」会被误读成覆盖不足
  · Windows 下 subprocess 必须显式 encoding='utf-8'
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent
TENANCY = ROOT / 'tenancy.py'
AUTH = ROOT / 'auth.py'
CITATION = ROOT / 'skills' / 'check_citation.py'
BILLING = ROOT / 'billing.py'
KB_API = ROOT / 'kb_api.py'

# 复制到临时工作区时要跳过的东西：venv 太大，.git / backups / .shots 没用，
# 数据库文件（*.db*）不带过去 —— 副本里让 app 自己建一个新的库，测试才干净。
# 🔴 但 data/ 目录本身要复制：retriever 的语料就在 data/ 里，少一个文件
#    就有 4 条 test_retriever 直接 FileNotFoundError，基线一开始就是红的。
IGNORE = shutil.ignore_patterns(
    '.git', 'venv', '.venv', '__pycache__', '.pytest_cache', '.shots',
    'backups', '*.db', '*.db-wal', '*.db-shm',
)

SCOPED_LINE = '    return model.query.filter_by(tenant_id=tenant_id)'

SELECT_THEN_UPDATE = (
    "    # MUTANT: 先 SELECT 查，再 UPDATE 写 —— 并发下必超扣\n"
    "    row = db.session.execute(\n"
    "        text('SELECT quota_used, quota_total FROM subscription"
    " WHERE id = :sid'),\n"
    "        {'sid': subscription_id},\n"
    "    ).fetchone()\n"
    "    if row.quota_used >= row.quota_total:\n"
    "        return False\n"
    "    db.session.execute(\n"
    "        text('UPDATE subscription SET quota_used = quota_used + 1"
    " WHERE id = :sid'),\n"
    "        {'sid': subscription_id},\n"
    "    )\n"
    "    return True\n"
)


MUTATIONS = [
    {
        'name': '1 scoped 去掉 tenant 过滤',
        'expect_min_red': 8, 'file': TENANCY, 'kind': 'replace',
        'old': SCOPED_LINE, 'new': '    return model.query  # MUTANT',
    },
    {
        'name': '2 scoped 恒用 tenant_id=1',
        'expect_min_red': 4, 'file': TENANCY, 'kind': 'replace',
        'old': SCOPED_LINE,
        'new': '    return model.query.filter_by(tenant_id=1)  # MUTANT',
    },
    {
        'name': '3 scoped 恒返回空',
        'expect_min_red': 5, 'file': TENANCY, 'kind': 'replace',
        'old': SCOPED_LINE,
        'new': '    return model.query.filter(db.false())  # MUTANT',
    },
    {
        'name': '4 关闭 ORM 层自动过滤',
        'expect_min_red': 1, 'file': TENANCY, 'kind': 'insert_before',
        'anchor': 'if state.is_column_load', 'text': 'return  # MUTANT',
    },
    {
        'name': '5 Tenant 过滤失效',
        'expect_min_red': 1, 'file': TENANCY, 'kind': 'replace_block',
        'old': ('    state.statement = state.statement.options(\n'
                '        _tenant_self_filter(tenant_id)\n'
                '    )'),
        'new': '    pass  # MUTANT',
    },
    {
        'name': '6 fetch_scoped 去掉隔离',
        'expect_min_red': 3, 'file': AUTH, 'kind': 'replace',
        'old': '    return scoped(model, current_tenant_id()).filter_by(id=record_id).first()',
        'new': '    return model.query.filter_by(id=record_id).first()  # MUTANT',
    },
    {
        'name': '7 豁免变后门（declares_no_answer 恒 True）',
        'expect_min_red': 2, 'file': CITATION, 'kind': 'replace',
        'old': 'def declares_no_answer(answer: str) -> bool:',
        'new': 'def declares_no_answer(answer: str) -> bool:\n    return True  # MUTANT',
    },
    {
        'name': '8 零引用不拦（ok 恒 True）',
        'expect_min_red': 2, 'file': CITATION, 'kind': 'replace',
        'old': '        result["ok"] = False\n        result["reason"] = "有证据但未引用"',
        'new': '        pass  # MUTANT: 零引用不拦',
    },
    {
        'name': '9 扣额度改「先查后减」（并发必超扣）',
        'expect_min_red': 2, 'file': BILLING, 'kind': 'replace_lines',
        'anchor': 'result = db.session.execute(',
        'until': 'return result.rowcount == 1',
        'new_text': SELECT_THEN_UPDATE,
    },
    {
        'name': '10 quota_used < quota_total 改成 <=',
        'expect_min_red': 1, 'file': BILLING, 'kind': 'replace',
        'old': "'WHERE id = :sid AND quota_used < quota_total'",
        'new': "'WHERE id = :sid AND quota_used <= quota_total'  # MUTANT",
    },
    {
        'name': '11 扣额度不检查 rowcount',
        'expect_min_red': 1, 'file': BILLING, 'kind': 'replace',
        'old': '    return result.rowcount == 1',
        'new': '    return True  # MUTANT: 不看 rowcount',
    },
    {
        'name': '12 降级的问答也扣额度',
        'expect_min_red': 3, 'file': BILLING, 'kind': 'replace',
        'old': '    if degraded:',
        'new': '    if False:  # MUTANT: 降级也扣',
    },
    {
        'name': '13 额度用完不特判（接口会 500）',
        'expect_min_red': 1, 'file': KB_API, 'kind': 'replace',
        'old': '        if isinstance(error, QuotaExceeded):',
        'new': '        if False:  # MUTANT: 不特判 -> 走通用 error',
    },
    {
        'name': '14 降级时截断已用量（凭空抹掉客户消耗）',
        'expect_min_red': 1, 'file': BILLING, 'kind': 'replace',
        'old': '        raise PlanDowngradeConflict(',
        'new': (
            "        # MUTANT: 错误做法 —— 凭空把已用量截断到新额度\n"
            "        subscription.quota_used = new_total\n"
            "        subscription.plan = new_plan\n"
            "        subscription.quota_total = new_total\n"
            "        db.session.commit()\n"
            "        return subscription\n"
            "    if False:\n"
            "        raise PlanDowngradeConflict("
        ),
    },
    {
        'name': '15 降级不检查已用量（产生 quota_used>quota_total）',
        'expect_min_red': 1, 'file': BILLING, 'kind': 'replace',
        'old': '    if new_total < subscription.quota_total and subscription.quota_used >= new_total:',
        'new': '    if False:  # MUTANT: 不检查',
    },
    {
        'name': '16 企业后台成员列表去掉租户过滤',
        'expect_min_red': 2, 'file': ROOT / 'admin_api.py', 'kind': 'replace',
        'old': '    rows = scoped(User, tenant_id).order_by(User.id).all()',
        'new': '    rows = User.query.order_by(User.id).all()  # MUTANT: 去掉隔离',
    },
    {
        'name': '17 改成员角色不校验租户',
        'expect_min_red': 1, 'file': ROOT / 'admin_api.py', 'kind': 'replace',
        'old': '    user = fetch_scoped(User, user_id)      # 🔴 跨租户 → None → 404',
        'new': '    user = User.query.filter_by(id=user_id).first()  # MUTANT',
    },
]


def run_tests(base=None):
    """在工作区里跑 pytest。base 缺省是项目根（只在手动调试时用到）。"""

    base = pathlib.Path(base or ROOT)
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests', '-q', '-p', 'no:warnings'],
        cwd=str(base), capture_output=True, text=True,
        encoding='utf-8', errors='replace', env=env,
    )
    return (proc.stdout or '') + (proc.stderr or '')


def summary_line(output):
    for line in reversed(output.splitlines()):
        s = line.strip()
        if re.search(r'\d+ passed', s) or re.search(r'\d+ failed', s):
            return s
    return ''


def count_red(output):
    m = re.search(r'(\d+) failed', summary_line(output))
    return int(m.group(1)) if m else 0


def count_passed(output):
    m = re.search(r'(\d+) passed', summary_line(output))
    return int(m.group(1)) if m else 0


def apply_mutation(spec):
    path = spec['file']
    original = path.read_text(encoding='utf-8')
    text = original
    kind = spec['kind']

    if kind == 'replace':
        if spec['old'] not in text:
            return None, original
        text = text.replace(spec['old'], spec['new'], 1)

    elif kind == 'replace_block':
        if spec['old'] not in text:
            return None, original
        text = text.replace(spec['old'], spec['new'], 1)

    elif kind == 'insert_before':
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if spec['anchor'] in line:
                indent = ' ' * (len(line) - len(line.lstrip(' ')))
                lines.insert(i, f'{indent}{spec["text"]}\n')
                text = ''.join(lines)
                break
        else:
            return None, original

    elif kind == 'replace_lines':
        lines = text.splitlines(keepends=True)
        start = None
        for i, line in enumerate(lines):
            if spec['anchor'] in line:
                start = i
                break
        if start is None:
            return None, original
        end = start
        while end < len(lines) and spec['until'] not in lines[end]:
            end += 1
        if end >= len(lines):
            return None, original
        end += 1
        lines[start:end] = [spec['new_text']]
        text = ''.join(lines)

    path.write_text(text, encoding='utf-8')
    return True, original


def run_one(spec, work):
    """跑单个变异。返回红数，或 None 表示变异无效。

    🔴 只在**临时工作区**里改文件：`spec['file']` 在 main() 里已经被改写成
    `<tmp>/xxx.py`，真实源码一个字节都不会动。所以这里不需要 backup_dir，
    还原就是把原文写回同一个文件（而且下一次 run_one 之前的 mutation 也只在
    临时目录里）。

    🔴 **三层自检**（第三层是审查建议补的）：
      1. 注入动作本身成功（模板匹配上了）
      2. 源码里确实出现了 MUTANT 标记
      3. **变异后的源码与原文必须不同，且能编译**

    第 3 层为什么必要：变异⑭ 第一版只删了 `raise`，但 `if` 条件还在，
    逻辑反而更严 —— 源码确实变了（MUTANT 标记也在），但**行为没变坏**，
    于是「0 红」被误读成「测试太弱」。
    注入不完整的变异会给出**假的安全感**，比不跑变异更危险。
    """

    path = spec['file']
    original_source = path.read_text(encoding='utf-8')

    def restore():
        path.write_text(original_source, encoding='utf-8')

    applied, _ = apply_mutation(spec)
    if not applied:
        restore()
        return None

    mutated_source = path.read_text(encoding='utf-8')

    # 自检 2：标记在
    if 'MUTANT' not in mutated_source:
        restore()
        return None

    # 自检 3a：源码真的变了
    if mutated_source == original_source:
        restore()
        return None

    # 自检 3b：语法没被改坏（语法坏的变异跑出来全是 import error，
    # 那一片红是假的 —— 不代表测试抓到了逻辑错误）
    try:
        compile(mutated_source, str(path), 'exec')
    except SyntaxError:
        restore()
        return None

    try:
        output = run_tests(work)
        red = count_red(output)
    finally:
        restore()
    return red


def make_workspace():
    """把项目复制到临时目录，变异只在副本上做。

    🔴 这是审查意见里的重点：老版本直接改真实 `tenancy.py` / `billing.py`，
    跑到一半 Ctrl-C 或进程被杀，仓库就留下一个带 MUTANT 的源文件 ——
    下一次 `git status` 才发现，而且很容易被顺手提交。
    复制副本之后再改，最坏情况也只是临时目录里有垃圾。
    """

    work = pathlib.Path(tempfile.mkdtemp(prefix='rmb01-mut-'))
    shutil.copytree(ROOT, work, dirs_exist_ok=True, ignore=IGNORE)
    # data/ 里是本机真实库，不复制；但 app 启动时不会自己建目录，
    # 少了它 SQLite 直接 "unable to open database file"
    (work / 'data').mkdir(parents=True, exist_ok=True)
    return work


def main():
    work = make_workspace()
    keep = os.environ.get('RMB01_MUT_KEEP') == '1'
    print(f'临时工作区: {work}')
    print('（真实源码不会被修改；RMB01_MUT_KEEP=1 可保留临时目录）')
    print()

    # 把 spec 里的绝对路径改指到副本上
    specs = []
    for spec in MUTATIONS:
        rel = spec['file'].relative_to(ROOT)
        specs.append({**spec, 'file': work / rel})

    try:
        baseline = run_tests(work)
        base = count_passed(baseline)
        print(f'基线: {base} passed')
        if base == 0:
            print('基线就没跑起来，先修基线再谈变异')
            return 1
        print()
        print('=' * 78)
        print('变异测试 —— 租户隔离 / 引用校验 / 计费原子性')
        print('=' * 78)

        results = []
        for spec in specs:
            red = run_one(spec, work)
            if red is None:
                print(f'  {spec["name"]:<40} 注入无效')
                results.append((spec['name'], 0, False))
                continue
            ok = red >= spec['expect_min_red']
            mark = 'OK' if ok else '不足'
            print(f'  {spec["name"]:<40} {red:>3} 红  (期望>={spec["expect_min_red"]})  {mark}')
            results.append((spec['name'], red, ok))

        final = run_tests(work)
        print()
        print(f'还原后: {count_passed(final)} passed')
    finally:
        if keep:
            print(f'保留临时目录: {work}')
        else:
            shutil.rmtree(work, ignore_errors=True)

    weak = [n for n, _, ok in results if not ok]
    if weak:
        print(f'覆盖不足的变异: {weak}')
        return 1
    print('所有变异都被测试抓到')
    return 0


if __name__ == '__main__':
    sys.exit(main())
