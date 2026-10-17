import os

import config
import qa
from flask import Flask, jsonify, render_template, request

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
    try:
        from billing import apply_sqlite_pragmas
        apply_sqlite_pragmas(db.engine)
    except Exception:
        application.logger.warning('sqlite_pragmas_failed')

    with application.app_context():
        db.create_all()

    _register_routes(application)
    return application


def _register_routes(application):
    @application.get('/healthz')
    def healthz():
        """健康检查：Docker HEALTHCHECK 打这里。

        只读配置快照，**不触发任何业务逻辑**，也不回显密钥。
        """

        return jsonify({'status': 'ok', 'service': 'rmb01', 'config': config.snapshot()})

    @application.get('/')
    def index():
        return render_template('index.html')

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

    from auth import register_auth_routes
    from kb_api import register_kb_routes

    register_auth_routes(application)
    register_kb_routes(application)


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
