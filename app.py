import os

import config
import qa
from flask import Flask, jsonify, redirect, render_template, request, url_for
from auth import current_user

from models import db


def create_app(overrides=None):
    """应用工厂。

    🔴 为什么要工厂而不是模块顶层直接 `app = Flask(__name__)`：
    1. 测试要注入**独立的内存库**（sqlite:///:memory:），否则测试会污染
       data/rmb01.db 业务库；
    2. 避免 import app 就连库 —— RMB03 那次 CI 红就是因为路径在
       **import 期**被固化，测试改了环境变量也不生效。

    overrides 里可以放SQLALCHEMY_DATABASE_URI 等，测试用它换库。
    """

    application = Flask(__name__)
    application.config['SQLALCHEMY_DATABASE_URI'] = config.DATABASE_URL
    application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    application.config['SECRET_KEY'] = config.SECRET_KEY or 'rmb01-ephemeral-dev-key'
    if overrides:
        application.config.update(overrides)

    db.init_app(application)

    # 🔴 SQLite 并发：WAL（读写不互斥）+ busy_timeout（第二个请求等待而不是立刻失败）。
    # 不设的话并发写会直接 'database is locked'。
    #
    # ⚠️ 必须在 app_context **内部**调：db.engine 绑定在应用上下文上，
    #    在外面调会抛异常（实测就是这里导致 sqlite_pragmas_failed 一直出现）。
    with application.app_context():
        _setup_sqlite_and_tables(application)

    _register_routes(application)
    return application


def _setup_sqlite_and_tables(application):
    """应用上下文内的初始化：先设 PRAGMA，再建表。

    🔴 **失败不能静默吞掉**。原实现 `except Exception: logger.warning(...)`
    有两个问题：
      1. 真实异常被丢弃，只留一句 warning，排查时看不到原因；
      2. 静默降级成「不用 WAL」—— 并发写会锁库，而调用方毫不知情。

    现在的处理：**记录完整异常 + 记录 PRAGMA 实际值**。
    WAL 设不上在 SQLite 里是可能的（比如某些网络文件系统），
    所以不直接崩溃，但必须让人看得见。
    """

    from billing import apply_sqlite_pragmas
    from models import db

    pragma_ok = True
    try:
        apply_sqlite_pragmas(db.engine)
    except Exception as exc:
        pragma_ok = False
        # 🔴 带上异常类型与原文，不然只剩一句「失败了」毫无信息
        application.logger.warning(
            'sqlite_pragmas_failed: %s: %s', type(exc).__name__, exc,
        )

    # 无论成功失败，都记录 PRAGMA 的**实际生效值** —— 这才是能定位问题的证据
    try:
        mode = db.session.execute(db.text('PRAGMA journal_mode')).scalar()
        timeout = db.session.execute(db.text('PRAGMA busy_timeout')).scalar()
        application.logger.info(
            'sqlite_pragma_state journal_mode=%s busy_timeout=%s applied=%s',
            mode, timeout, pragma_ok,
        )
        if not pragma_ok or str(mode).lower() != 'wal':
            application.logger.warning(
                'sqlite_wal_unavailable: 当前 journal_mode=%s（期望 wal）。'
                '并发写可能报 database is locked —— '
                '生产环境应改用 PostgreSQL。', mode,
            )
    except Exception:
        application.logger.exception('sqlite_pragma_probe_failed')

    db.create_all()



def _register_routes(application):
    @application.get('/healthz')
    def healthz():
        """健康检查：Docker HEALTHCHECK 打这里。

        只读配置快照，**不触发任何业务逻辑**，也不回显密钥。
        """

        return jsonify({'status': 'ok', 'service': 'rmb01', 'config': config.snapshot()})

    @application.get('/')
    def index():
        """🔴 首页跳转，不再渲染旧的题库页。

        阶段 6 起 `/` 是入口：已登录去知识库，未登录去登录页。
        `templates/index.html`（XM03 的题库页）不再挂在根路径 ——
        它和 RMB01 的知识库产品无关，留着只会让评审看到错误的首页。
        """
        if current_user() is None:
            return redirect(url_for('page_login'))
        return redirect(url_for('page_kbs'))

    @application.post('/api/ask')
    def ask_question():
        question, error_response = _extract_question()
        if error_response is not None:
            return error_response
        thread_id, error_response = _extract_thread_id()
        if error_response is not None:
            return error_response

        try:
            result = _run_configured_backend(question, thread_id=thread_id)
        except Exception:
            application.logger.exception('问答接口处理失败')
            return jsonify({'answer': '服务暂时不可用，请稍后重试', 'references': []})

        return jsonify(result)

    @application.post('/api/agent/ask')
    def ask_agent_question():
        question, error_response = _extract_question()
        if error_response is not None:
            return error_response
        thread_id, error_response = _extract_thread_id()
        if error_response is not None:
            return error_response

        try:
            if thread_id is None:
                result = _run_agent(question)
            else:
                result = _run_agent(question, thread_id=thread_id)
        except Exception:
            application.logger.exception('Agent 接口处理失败')
            return jsonify({'answer': '服务暂时不可用，请稍后重试', 'references': []})

        return jsonify(result)

    from admin_api import register_admin_routes
    from auth import register_auth_routes
    from kb_api import register_kb_routes

    register_auth_routes(application)
    register_kb_routes(application)
    from pages import register_page_routes

    register_admin_routes(application)
    register_page_routes(application)


def _get_qa_module():
    if config.use_langchain():
        import qa_lc

        return qa_lc
    return qa


def _extract_question():
    payload = request.get_json(silent=True)
    question = payload.get("question") if isinstance(payload, dict) else None

    if not isinstance(question, str) or not question.strip():
        return None, (jsonify({"error": "问题不能为空"}), 400)

    question = question.strip()
    if len(question) > 500:
        return None, (jsonify({"error": "问题过长"}), 400)

    return question, None


def _extract_thread_id():
    payload = request.get_json(silent=True)
    thread_id = payload.get("thread_id") if isinstance(payload, dict) else None

    if thread_id is None:
        return None, None
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None, (jsonify({"error": "thread_id 无效"}), 400)

    return thread_id.strip(), None


def _run_agent(question, thread_id=None):
    import agent

    if thread_id is None:
        return agent.run_agent(question)
    return agent.run_agent(question, thread_id=thread_id)


def _run_configured_backend(question, thread_id=None):
    if config.use_agent():
        if thread_id is None:
            return _run_agent(question)
        return _run_agent(question, thread_id=thread_id)
    return _get_qa_module().ask(question)


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=config.APP_PORT)
