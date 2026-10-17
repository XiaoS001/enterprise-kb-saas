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
  · 每个变异跑完立刻还原，不等下一轮开头清理
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
]


def run_tests():
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests', '-q', '-p', 'no:warnings'],
        cwd=str(ROOT), capture_output=True, text=True,
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


def run_one(spec, backup_dir):
    path = spec['file']
    pristine = backup_dir / path.name
    shutil.copy(pristine, path)
    applied, _ = apply_mutation(spec)
    if not applied:
        shutil.copy(pristine, path)
        return None
    if 'MUTANT' not in path.read_text(encoding='utf-8'):
        shutil.copy(pristine, path)
        return None
    output = run_tests()
    red = count_red(output)
    shutil.copy(pristine, path)
    return red


def main():
    backup_dir = pathlib.Path(tempfile.mkdtemp())
    paths = sorted({m['file'] for m in MUTATIONS}, key=lambda p: p.name)
    for p in paths:
        shutil.copy(p, backup_dir / p.name)

    baseline = run_tests()
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
    try:
        for spec in MUTATIONS:
            red = run_one(spec, backup_dir)
            if red is None:
                print(f'  {spec["name"]:<40} 注入无效')
                results.append((spec['name'], 0, False))
                continue
            ok = red >= spec['expect_min_red']
            mark = 'OK' if ok else '不足'
            print(f'  {spec["name"]:<40} {red:>3} 红  (期望>={spec["expect_min_red"]})  {mark}')
            results.append((spec['name'], red, ok))
    finally:
        for p in paths:
            shutil.copy(backup_dir / p.name, p)
        shutil.rmtree(backup_dir, ignore_errors=True)

    final = run_tests()
    print()
    print(f'还原后: {count_passed(final)} passed')

    weak = [n for n, _, ok in results if not ok]
    if weak:
        print(f'覆盖不足的变异: {weak}')
        return 1
    print('所有变异都被测试抓到')
    return 0


if __name__ == '__main__':
    sys.exit(main())