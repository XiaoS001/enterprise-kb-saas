"""RMB01 配置 —— 全部走环境变量，密钥不落代码。

移植自 RMB03 的 config.py（同一个模式），解决的问题：

XM03 的 agent.py 里有 **4 处**散落的
    os.environ.get("DEEPSEEK_API_KEY")
（第 252/ 407 / 465 / 623 行）。散落的直接后果：
  · 改配置来源要改 4 个地方，漏一处就行为不一致
  · 测试想改 key 必须 monkeypatch os.environ，且**受 import 期缓存影响**
    （RMB03 就踩过这个坑：本地全绿但 CI 红了）
  · 「key 从哪来」没有单一答案

所以这里收口成一处，agent.py 全部改读 config.DEEPSEEK_API_KEY。
config 本身在 **import 期**读取环境变量 —— 这是 Flask 应用的惯例，
测试要覆盖它就用 monkeypatch.setattr(config, ...)（改属性而非环境变量）。
"""

import os
from pathlib import Path

# ---- 本地 .env（可选） ------------------------------------------------------
# 用途：本机演示的密钥/模型端点不进代码、不进 git（.gitignore 已排除 .env）。
# 规则：用 os.environ.setdefault —— 真环境变量永远优先于 .env 文件。
_ENV_FILE = Path(__file__).resolve().parent / '.env'
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding='utf-8').splitlines():
        _line = _line.strip()
        if not _line or _line.startswith('#') or '=' not in _line:
            continue
        _k, _, _v = _line.partition('=')
        os.environ.setdefault(_k.strip(), _v.strip())


def _env(name, default=''):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name, default=False):
    value = _env(name, '').lower()
    if not value:
        return default
    return value in ('1', 'true', 'yes', 'on')


def _env_float(name, default=0.0):
    """读浮点环境变量。非法值退回默认而不是启动失败。"""

    try:
        return float(_env(name, default))
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(__file__).resolve().parent

# --- 应用 ---
APP_HOST = _env('RMB01_HOST', '127.0.0.1')
APP_PORT = int(_env('RMB01_PORT', '5001') or 5001)
LOG_LEVEL = _env('RMB01_LOG_LEVEL', 'INFO').upper()
SECRET_KEY = _env('RMB01_SECRET_KEY', '')
SECRET_KEY_IS_EPHEMERAL = not SECRET_KEY
# 密码哈希盐。与 SECRET_KEY 同理：不给硬编码默认值 ——
# 悄悄用一个固定开发盐，等于所有部署共用同一把钥匙。
PASSWORD_SALT = _env('RMB01_PASSWORD_SALT', '')

# --- 数据 ---
DATA_DIR = _env('RMB01_DATA_DIR', str(BASE_DIR / 'data'))
DATABASE_URL = _env('RMB01_DATABASE_URL',
                    'sqlite:///' + str(Path(DATA_DIR) / 'rmb01.db'))
BACKUP_DIR = _env('RMB01_BACKUP_DIR', str(BASE_DIR / 'backups'))
TIMEZONE = _env('RMB01_TIMEZONE', 'Asia/Shanghai')

# --- 模型（🔴 key 只从环境变量取，绝不硬编码）---
DEEPSEEK_API_KEY = _env('DEEPSEEK_API_KEY', '')
AI_ENABLED = _env_bool('RMB01_AI_ENABLED', True)
AI_MODEL = _env('RMB01_AI_MODEL', 'deepseek-chat')
AI_BASE_URL = _env('RMB01_AI_BASE_URL', 'https://api.deepseek.com')


def api_key() -> str:
    """🔴 **运行时**读取 key，测试用 monkeypatch.setenv 即可生效。

    为什么不能只用模块级常量（DEEPSEEK_API_KEY）：
    XM03 的 13 处测试用 `monkeypatch.setenv("DEEPSEEK_API_KEY", ...)`。
    模块级常量在 import 期固化，之后改环境变量对它无效 —— 实测会让
    9 条基线测试挂掉（它们以为设了 key，实际读到的还是空）。

    所以：常量保留给「启动时打印配置」这类场景；
    真正判断「有没有 key」的地方一律调 api_key()，它每次重读环境变量。
    """

    return _env('DEEPSEEK_API_KEY', '')

# --- 检索 ---
# 🔴 向量检索默认**关闭**。原因（任务单第四节）：
#    嵌入模型 BAAI/bge-small-zh-v1.5 是运行时从 HuggingFace 下载的（约 90MB），
#    仓库不预置。默认开会让离线/ 容器首次运行失败，且 CI 会被下载拖慢。
#    BM25 零网络依赖，作为默认路径。
VECTOR_ENABLED = _env_bool('RMB01_VECTOR_ENABLED', False)
EMBEDDING_MODEL = _env('RMB01_EMBEDDING_MODEL', 'BAAI/bge-small-zh-v1.5')
EMBEDDING_DIM = 512

# BM25 检索参数（阶段 3 建索引时用）
BM25_TOP_K = int(_env('RMB01_BM25_TOP_K', '5') or 5)
CHUNK_SEPARATOR = _env('RMB01_CHUNK_SEPARATOR', '\n\n')
CHUNK_MAX_CHARS = int(_env('RMB01_CHUNK_MAX_CHARS', '800') or 800)

# 🔴 BM25 相关性阈值（相对最高分的比例）。
#   小于这个比例的召回结果直接丢掉 —— 防止「怎么」「如何」这类
#   高频疑问词把不相关段落拉进引用。0.35 是实测调出来的：
#   「慢查询怎么排查」在 0.35 时只留下真正相关的那段。
BM25_RELATIVE_THRESHOLD = _env_float('RMB01_BM25_RELATIVE_THRESHOLD', 0.35)


def _env_bool_compat(*names, default=False):
    """依次尝试多个环境变量名，返回第一个命中的。

    🔴 为什么需要兼容：XM03 原本用 USE_AGENT / USE_LANGCHAIN 这两个名字，
    而且**基线测试用 monkeypatch.setenv 设它们**（tests/test_agent.py 等）。
    一旦改名，那些测试就会失效 —— 阶段 2 的验收明确要求「53 条基线不能退化」。

    所以新名 RMB01_* 优先，旧名保留为回退。
    """

    for name in names:
        value = _env(name, '').lower()
        if value:
            return value in ('1', 'true', 'yes', 'on')
    return default


def use_agent() -> bool:
    """是否走 LangGraph 多智能体（默认关闭，走简单 BM25 检索）。

    🔴 做成函数而非常量：测试用 monkeypatch.setenv 即可生效。
    与 api_key() 同一个理由 —— import 期固化会让测试改不动。
    """

    return _env_bool_compat('RMB01_USE_AGENT', 'USE_AGENT', default=False)


def use_langchain() -> bool:
    """问答链路是否走 LangChain 版（默认关闭）。"""

    return _env_bool_compat('RMB01_USE_LANGCHAIN', 'USE_LANGCHAIN', default=False)


def snapshot():
    """配置的安全视图，用于 /healthz 与启动日志。

    只暴露「开没开 / 指向什么」，**绝不回显密钥本体**。
    """

    return {
        'app_port': APP_PORT,
        'log_level': LOG_LEVEL,
        'database_driver': DATABASE_URL.split(':', 1)[0],
        'secret_key_ephemeral': SECRET_KEY_IS_EPHEMERAL,
        'ai_enabled': AI_ENABLED,
        'ai_model': AI_MODEL,
        'ai_key_present': bool(api_key()),
        'vector_enabled': VECTOR_ENABLED,
        'embedding_model': EMBEDDING_MODEL,
        'use_agent': use_agent(),
        'use_langchain': use_langchain(),
    }