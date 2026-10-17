"""降级标记的诚实性测试。

🔴 这组测试盯的是 `agent._empty_result` 曾经硬编码 degraded=False
导致「降级了却不承认」的问题。

为什么重要：不只是显示问题 —— 阶段 4 的额度扣减按 degraded 判断
该不该计费，标记错了就**算错账**。
"""
import pytest

import agent


@pytest.fixture()
def no_key(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)


def test_empty_result_defaults_to_not_degraded():
    """默认参数下不声称降级。"""

    r = agent._empty_result('随便什么')

    assert r['degraded'] is False
    assert r['degradation_reasons'] == []


def test_empty_result_accepts_degraded_flag():
    r = agent._empty_result('x', degraded=True, reason='因为某事')

    assert r['degraded'] is True
    assert r['degradation_reasons'] == ['因为某事']


def test_missing_api_key_is_marked_degraded(no_key):
    """🔴 没配 key 是**实质降级**：应该调模型而没调成。"""

    r = agent.run_agent('连接池怎么配置')

    assert 'API' in r['answer'] or '密钥' in r['answer']
    assert r['degraded'] is True, '🔴 降级了必须承认'
    assert r['degradation_reasons'], '降级必须有原因'


def test_degraded_flag_not_hardcoded_in_source():
    """🔴 防回归：源码里不该再出现裸的 "degraded": False 硬编码。

    这条用源码扫描而不是行为断言 —— 因为「哪些早退点该标降级」
    很容易在改代码时漏掉某个分支，行为测试覆盖不全。
    源码里出现硬编码 False 就是明确的信号。
    """

    import inspect
    import re

    source = inspect.getsource(agent._empty_result)

    # 🔴 用正则而不是字面匹配：字面匹配 '"degraded": False' 会在有人写成
    #    '"degraded":False'（无空格）或改用单引号时**假通过** ——
    #    而那恰恰是最该被拦住的情况。
    hardcoded = re.search(r"""["']degraded["']\s*:\s*False""", source)
    assert hardcoded is None, (
        '_empty_result 又出现了硬编码 degraded=False —— '
        '应该改成 degraded 参数传入'
    )
    assert re.search(r"""["']degraded["']\s*:\s*degraded""", source), (
        '_empty_result 应该用参数而不是硬编码'
    )


def test_empty_question_is_not_degraded():
    """空问题是参数错误，不该算降级（压根没开始跑）。"""

    r = agent.run_agent('   ')

    assert r['answer'] == '问题不能为空'
    assert r['degraded'] is False


def test_graph_crash_is_marked_degraded(monkeypatch, tmp_path):
    """图崩了也是降级。"""

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')

    class Boom:
        def invoke(self, *args, **kwargs):
            raise RuntimeError('图崩了')

    monkeypatch.setattr(agent, 'AGENT_GRAPH', Boom())

    r = agent.run_agent('随便问个问题')

    assert r['degraded'] is True
    assert any('图崩了' in x for x in r['degradation_reasons'])


def test_every_empty_result_path_declares_its_status(no_key):
    """🔴 穷举早退路径，确认每条都说清自己是不是降级。"""

    # 空问题 → 参数错误
    empty_q = agent.run_agent('')
    assert empty_q['degraded'] is False, '空问题不该算降级'

    # 无 key → 实质降级
    no_api = agent.run_agent('问题')
    assert no_api['degraded'] is True, '无 key 必须算降级'