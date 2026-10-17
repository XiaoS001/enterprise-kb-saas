"""引用校验（RMB01 定制行为）测试。

三组：
  1. 格式对齐 —— [doc:N] 必须能提取出来（这是阶段 3 之前的一个静默失效）
  2. 判定规则 —— 有证据零引用不合格；声明查无则豁免
  3. 可自愈性 —— 零引用不可重试（否则 agent 死循环）
"""
import pytest

from skills.check_citation import (
    check_citation,
    declares_no_answer,
    extract_citation_ids,
)


# ============ 1. 格式对齐 ============
@pytest.mark.parametrize("text, expected", [
    ('依据 [12]', ['12']),              # XM03 原格式（保留）
    ('依据 [doc:12]', ['12']),          # 🔴 RMB01 新格式
    ('依据【doc:12】', ['12']),          # 中文括号
    ('依据 [doc-12]', ['12']),          # 连字符
    ('依据 [doc_12]', ['12']),          # 下划线
    ('见 [Q4]', ['4']),                 # XM03 的 Q 前缀
    ('见 [doc:2946] 与 [doc:2947]', ['2946', '2947']),
])
def test_extract_citations_formats(text, expected):
    assert extract_citation_ids(text) == expected


def test_doc_format_is_not_silently_ignored():
    """🔴 回归测试：这个洞真实存在过。

    XM03 的正则只认 [12]，不认 [doc:12] → 提取到 [] →
    citations=[] → invalid=[] → ok=True，引用校验静默失效。
    """

    assert extract_citation_ids('依据 [doc:2946]') == ['2946']


def test_url_fragment_is_not_a_citation():
    """🔴 不能把 URL 片段误判成引用。"""

    assert extract_citation_ids('见 https://example.com/page/12 说明') == []


# ============ 2. 判定规则 ============
def test_valid_citation_passes():
    r = check_citation('依据 [doc:2946]', [{'id': '2946'}])

    assert r['ok'] is True
    assert r['citations'] == ['2946']
    assert r['invalid_citations'] == []


def test_unknown_doc_citation_is_invalid():
    """🔴 引用了不存在的 doc_id → ok=False。"""

    r = check_citation('依据 [doc:999]', [{'id': '2946'}])

    assert r['ok'] is False
    assert r['invalid_citations'] == ['999']


def test_answer_without_citation_is_rejected_when_evidence_exists():
    """🔴 有证据但答案零引用 → 不合格（原实现会放行）。"""

    r = check_citation('答案是编的，完全没有引用任何东西。', [{'id': '2946'}])

    assert r['ok'] is False
    assert r['reason'] == '有证据但未引用'


def test_declaring_no_answer_is_exempt():
    """明确说「查不到」时允许零引用。"""

    r = check_citation('知识库中没有相关内容。', [{'id': '2946'}])

    assert r['ok'] is True


def test_no_evidence_and_no_citation_is_ok():
    """既没证据也没引用 —— 正常情况。"""

    r = check_citation('没有找到相关内容。', [])

    assert r['ok'] is True


def test_exemption_is_not_a_backdoor():
    """🔴 豁免必须要求明确措辞，不能随口一句就绕过。"""

    # 这些话术没有命中豁免清单
    for vague in ['不知道', '无法回答', '抱歉', '资料不足', '略']:
        r = check_citation(vague, [{'id': '2946'}])
        assert r['ok'] is False, f'「{vague}」不该被当成豁免'


def test_declares_no_answer_recognized_phrases():
    assert declares_no_answer('知识库中没有相关内容') is True
    assert declares_no_answer('没有找到相关内容。') is True
    assert declares_no_answer('随便说点什么') is False


# ============ 3. 可自愈性（防死循环）============
def test_zero_citation_is_not_retryable():
    """🔴 零引用重试也自愈不了，必须标记为不可重试。

    否则 agent 会在 answerer→critic→answerer 之间死循环，
    直到撞 GRAPH_RECURSION_LIMIT（实测踩过）。
    """

    r = check_citation('答案没有任何引用。', [{'id': '2946'}])

    assert r['ok'] is False
    assert r.get('retryable') is False


def test_invalid_citation_is_retryable():
    """引用了不存在的证据 → 换查询重来有意义 → 可重试。"""

    r = check_citation('依据 [doc:999]', [{'id': '2946'}])

    assert r['ok'] is False
    assert r.get('retryable', True) is True, '引用无效时应可重试'


def test_ok_result_has_no_retryable_flag():
    """合格结果不该带 retryable 字段（避免调用方误读）。"""

    r = check_citation('依据 [doc:1]', [{'id': '1'}])

    assert 'retryable' not in r


# ============ 4. agent 集成（防死循环的回归）============
def test_agent_does_not_loop_on_zero_citation(monkeypatch):
    """🔴 端到端回归：fake model 一直不引用时，agent 必须停，不能撞递归上限。

    这是第 3 组测试的实际意义 —— 单测只验字段，这里验「真的不会死循环」。
    """

    import agent

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    calls = {'search': 0}

    def fake_search(query, top_k=5):
        calls['search'] += 1
        return [{'id': 190, 'stem': query, 'answer': 'x', 'type': 'choice'}]

    monkeypatch.setattr(agent.retriever_lc, 'search', fake_search)

    def fake_model(prompt, variables, api_key):
        if prompt is agent.EVALUATE_PROMPT:
            return '{"enough": true, "reason": "ok"}'
        # 🔴 故意不引用任何东西 —— 这在改retryable 之前会导致死循环
        return '答案内容，但一个引用都没给。'

    monkeypatch.setattr(agent, '_call_model', fake_model)

    # 不指定 thread_id（不走 checkpointer），避免跨轮状态干扰
    result = agent.run_agent('测试问题')

    assert result['answer'], '应该正常返回答案'
    assert calls['search'] >= 1, '应该做过检索'