"""模型调用的端点参数契约测试。

🔴 这组测试盯的是一个**只有真调才会暴露**的 bug：

    agent.py   DEEPSEEK_MODEL = "deepseek-chat"        # 硬编码
               ChatDeepSeek(model=..., api_key=..., timeout=...)
                                                    # ↑ 没传 api_base

`config.AI_MODEL` 与 `config.AI_BASE_URL` 虽然定义了，**但模型调用完全没读它们**。
不传 `api_base`，`ChatDeepSeek` 默认打官方 `api.deepseek.com` ——
换任何第三方 OpenAI 兼容端点都必然 401。

而 401 看起来像「key 错了」，所以很容易往 key 上找原因。
287 条测试全绿也发现不了：它们全是 mock，根本走不到真实构造。

同一处 bug 在三处存在：`agent._call_model`、`qa_lc._invoke_model`、`qa.ask`。
"""
import pytest

import config
import qa_lc

# qa_lc.PROMPT 需要这两个变量
_VARS = {'question': '问题', 'materials': '材料'}


class _FakeResponse:
    def __init__(self, content='模型写的答案 [doc:1]'):
        self.content = content


def _make_fake_chat():
    """造一个可被 `prompt | model` 使用的假 ChatDeepSeek。

    🔴 必须继承 Runnable：`ChatPromptTemplate | model` 要求右侧是 Runnable，
    否则 langchain 会在管道运算处抛错，**构造参数还没被断言就先炸了**。
    继承后 `|` 得到 RunnableSequence，`.invoke()` 能正常走完。
    """

    from langchain_core.runnables import Runnable

    class _FakeChatDeepSeek(Runnable):
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            _FakeChatDeepSeek.instances.append(kwargs)

        def invoke(self, input, config=None, **kwargs):
            return _FakeResponse()

    return _FakeChatDeepSeek


@pytest.fixture()
def captured(monkeypatch):
    """换掉 ChatDeepSeek，并给 config 一组**非默认**值。

    🔴 用非默认值很关键：如果断言写成 `== 'deepseek-chat'`（默认值），
    那即使代码根本没读 config、仍在用硬编码常量，测试也会通过。
    """

    fake = _make_fake_chat()
    monkeypatch.setattr('agent.ChatDeepSeek', fake)
    monkeypatch.setattr('qa_lc.ChatDeepSeek', fake)

    monkeypatch.setattr(config, 'AI_MODEL', 'DeepSeek-V4-Flash')
    monkeypatch.setattr(config, 'AI_BASE_URL',
                        'https://developer.amd.com.cn/radeon/api/v1')
    return fake


# ============ agent._call_model ============
def test_agent_passes_api_base(captured):
    """🔴 不传 api_base 就默认打官方端点 —— 这就是 401 的真正原因。"""

    import agent

    agent._call_model(qa_lc.PROMPT, _VARS, 'sk-test')

    assert captured.instances, 'ChatDeepSeek 应该被构造'
    kwargs = captured.instances[0]

    assert 'api_base' in kwargs, (
        '🔴 ChatDeepSeek 没收到 api_base —— 会默认打 api.deepseek.com'
    )
    assert kwargs['api_base'] == config.AI_BASE_URL
    assert kwargs['api_base'] == 'https://developer.amd.com.cn/radeon/api/v1'


def test_agent_reads_model_from_config(captured):
    """🔴 model 必须读 config，不能是硬编码的 deepseek-chat。"""

    import agent

    agent._call_model(qa_lc.PROMPT, _VARS, 'sk-test')

    kwargs = captured.instances[0]
    assert kwargs['model'] == config.AI_MODEL
    assert kwargs['model'] == 'DeepSeek-V4-Flash'
    assert kwargs['model'] != 'deepseek-chat', '🔴 仍在用硬编码的 deepseek-chat'


def test_agent_still_passes_api_key_and_timeout(captured):
    import agent

    agent._call_model(qa_lc.PROMPT, _VARS, 'sk-abc123')

    kwargs = captured.instances[0]
    assert kwargs['api_key'] == 'sk-abc123'
    assert kwargs['timeout'] == agent.REQUEST_TIMEOUT


# ============ qa_lc._invoke_model（同一 bug）============
def test_qa_lc_passes_api_base_and_model(captured):
    import qa_lc as mod

    mod._invoke_model('问题', '材料', 'sk-test')

    assert captured.instances, 'ChatDeepSeek 应该被构造'
    kwargs = captured.instances[0]
    assert kwargs.get('api_base') == config.AI_BASE_URL
    assert kwargs.get('model') == config.AI_MODEL


# ============ qa.ask（裸 requests，端点要自己拼）============
def test_qa_builds_chat_completions_url_from_base(monkeypatch):
    """🔴 base 是根路径时，要补 /chat/completions。"""

    import qa

    monkeypatch.setattr(config, 'AI_BASE_URL',
                        'https://developer.amd.com.cn/radeon/api/v1')

    assert qa._chat_completions_url() == (
        'https://developer.amd.com.cn/radeon/api/v1/chat/completions'
    )


def test_qa_url_not_double_suffixed(monkeypatch):
    """base 已含 /chat/completions 时不能再拼一遍（否则 404）。"""

    import qa

    monkeypatch.setattr(
        config, 'AI_BASE_URL', 'https://example.test/v1/chat/completions')

    url = qa._chat_completions_url()

    assert url == 'https://example.test/v1/chat/completions'
    assert url.count('/chat/completions') == 1


def test_qa_posts_to_config_base_and_model(monkeypatch):
    """🔴 断言真正 POST 的 URL 与 body.model 都来自 config。"""

    import qa

    monkeypatch.setattr(config, 'AI_MODEL', 'DeepSeek-V4-Flash')
    monkeypatch.setattr(config, 'AI_BASE_URL',
                        'https://developer.amd.com.cn/radeon/api/v1')
    monkeypatch.setattr(config, 'api_key', lambda: 'sk-test')

    # 不让它真去检索题库
    monkeypatch.setattr(qa.retriever, 'search', lambda q, top_k=5: [
        {'id': 1, 'stem': '题干', 'answer': '标准答案',
         'options': [], 'explanation': ''},
    ])

    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': '答案 [doc:1]'}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen['url'] = url
        seen['body'] = json
        return _Resp()

    monkeypatch.setattr(qa.requests, 'post', fake_post)

    qa.ask('问题', top_k=1)

    assert seen['url'] == (
        'https://developer.amd.com.cn/radeon/api/v1/chat/completions'
    ), f'🔴 POST 到了 {seen["url"]}'
    assert seen['body']['model'] == 'DeepSeek-V4-Flash'


# ============ 结构断言：不许再出现硬编码 ============
def test_no_hardcoded_model_in_call_sites():
    """🔴 调用点不许再出现 `model=DEEPSEEK_MODEL`。"""

    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ('agent.py', 'qa_lc.py'):
        src = (root / name).read_text(encoding='utf-8')
        code = '\n'.join(
            line for line in src.splitlines()
            if not line.strip().startswith('#')
        )
        assert 'model=DEEPSEEK_MODEL' not in code, (
            f'🔴 {name} 里还有 model=DEEPSEEK_MODEL 硬编码'
        )


def test_agent_call_model_source_uses_config():
    """agent._call_model 源码里必须出现 api_base= 且读 config。"""

    import inspect
    import agent

    src = inspect.getsource(agent._call_model)

    assert 'api_base=' in src, '🔴 _call_model 没有传 api_base'
    assert 'AI_BASE_URL' in src, '🔴 _call_model 没读 config.AI_BASE_URL'
    assert 'AI_MODEL' in src, '🔴 _call_model 没读 config.AI_MODEL'