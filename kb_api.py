"""🔴 知识库路由 —— 从零做（XM03 没有这层，它只有一个写死的 questions.json）。

三条设计原则：

1. **每个返回租户数据的接口，都有配套的跨租户测试**（见 tests/test_kb_api.py）
2. **跨租户一律 404**，不返 403 —— 403 会泄漏「这个资源存在」
3. **写侧 tenant_id 只从会话取**，请求体里的一律丢弃（tenant_fields 强制覆盖）
"""
import logging

from flask import jsonify, request

import config
from auth import current_user, current_tenant_id, fetch_scoped, login_required
from billing import QuotaExceeded
from chunking import build_document_chunks
from kb_search import format_citations, search_documents
from models import Document, Kb, db
from tenancy import tenant_fields


logger = logging.getLogger('rmb01.kb')


# ============ Kb CRUD ============
def list_kbs():
    """🔴 只返回**当前租户**的知识库（scoped 是唯一入口）。"""

    from tenancy import scoped

    tenant_id = current_tenant_id()
    rows = scoped(Kb, tenant_id).order_by(Kb.id).all()
    return [_kb_json(k) for k in rows]


def get_kb(kb_id):
    """按 id 取知识库；**跨租户取不到就当作不存在**（上层返 404）。"""

    kb = fetch_scoped(Kb, kb_id)
    if kb is None:
        return None
    return _kb_json(kb)


def create_kb(payload):
    """创建知识库。

    🔴 tenant_id 只能从会话取：请求体里塞了也会被 tenant_fields 丢弃。
    """

    data = tenant_fields(payload or {}, current_tenant_id())
    name = (data.get('name') or '').strip()
    if not name:
        return None, 'name 不能为空'

    kb = Kb(
        tenant_id=data['tenant_id'],
        name=name,
        description=(data.get('description') or '').strip() or None,
    )
    db.session.add(kb)
    db.session.commit()
    return _kb_json(kb), None


def delete_kb(kb_id):
    """删除知识库（文档随库级联删除）。跨租户取不到 → None。"""

    kb = fetch_scoped(Kb, kb_id)
    if kb is None:
        return None
    db.session.delete(kb)
    db.session.commit()
    return True


# ============ 文档 ============
def list_documents(kb_id):
    """列出某库的文档分段。库不属于本租户 → None（404）。"""

    kb = fetch_scoped(Kb, kb_id)
    if kb is None:
        return None
    rows = _documents_query(kb_id).order_by(Document.id).all()
    return [_doc_json(d) for d in rows]


def _documents_query(kb_id):
    """按 kb_id 取文档（仍经 scoped 保证租户隔离）。"""

    from tenancy import scoped

    return scoped(Document, current_tenant_id()).filter_by(kb_id=kb_id)


def upload_document(kb_id, title, content, separator=None, max_chars=None):
    """上传文档 → 分段 → 落库。库不属于本租户 → None（404）。

    🔴 tenant_id 由tenant_fields 强制覆盖：即使调用方传了别的 tenant_id，
    落库的仍是当前租户 —— 这是阶段 2 写侧隔离的延续。
    """

    kb = fetch_scoped(Kb, kb_id)
    if kb is None:
        # (None, None) = 「库不属于本租户」；与「参数不对」的 (None, error) 区分
        return None, None

    title = (title or '').strip()
    if not title:
        return None, 'title 不能为空'
    if not content or not content.strip():
        return None, 'content 不能为空'

    chunks = chunk_text_safe(content, separator, max_chars)
    if not chunks:
        return None, 'content 分段后为空'

    user = current_user()
    documents = []
    for index, piece in enumerate(chunks):
        doc = Document(
            tenant_id=kb.tenant_id,     # ← 用库的租户，不是请求里的
            kb_id=kb_id,
            uploader_id=user.id if user else None,
            title=title,
            chunk_index=index,
            content=piece,
        )
        db.session.add(doc)
        documents.append(doc)

    db.session.commit()
    return [_doc_json(d) for d in documents], None


def chunk_text_safe(content, separator=None, max_chars=None):
    """分段（薄封装，便于测试注入参数）。"""

    from chunking import chunk_text

    return chunk_text(content, separator=separator, max_chars=max_chars)


def delete_document(document_id):
    doc = fetch_scoped(Document, document_id)
    if doc is None:
        return None
    db.session.delete(doc)
    db.session.commit()
    return True


# ============ 问答 ============
def ask_in_kb(kb_id, question, top_k=3, use_agent=False):
    """🔴 在指定知识库里问答。

    返回 (result, error)：
      · kb不属于本租户 → (None, None)  ← 上层转 404
      · 参数不对       → (None, error)
      · 正常           → (result, None)

    result 里的references 指向**具体的 document + 段落**，
    前端可以显示「依据《XX文档》第 N 段」。
    """

    kb = fetch_scoped(Kb, kb_id)
    if kb is None:
        return None, None

    question = (question or '').strip()
    if not question:
        return None, 'question 不能为空'
    if len(question) > 500:
        return None, '问题过长'

    tenant_id = current_tenant_id()
    hits = search_documents(kb_id, question, top_k=top_k, tenant_id=tenant_id)

    if not hits:
        # 🔴 检索不到就说检索不到，不硬答 —— 这是引用校验能成立的前提。
        #    degraded 标True：调用方要能区分「查到了但降级」与
        #    「压根没查到相关内容」，后者同样不是一次成功的问答。
        return {
            'answer': '知识库中没有相关内容。',
            'references': [],
            'citations': [],
            'invalid_citations': [],
            'degraded': True,
            'degradation_reasons': ['检索未命中任何文档分段'],
            'retrieved_count': 0,
        }, None

    references = [
        {
            'document_id': hit['document_id'],
            'title': hit['title'],
            'chunk_index': hit['chunk_index'],
            'score': hit.get('score'),
            'preview': hit['content'][:120],
        }
        for hit in hits
    ]

    evidence = format_citations(hits)
    answer = ''
    degraded = True
    degradation_reasons = []

    if use_agent:
        # 走agent（LangGraph 多智能体），它会自己再检索+生成+校验引用
        answer, degradation_reasons = _ask_via_agent(question, evidence)
        degraded = bool(degradation_reasons) or not answer
        if not answer and not degradation_reasons:
            degradation_reasons = ['agent 未产出可用答案']
    else:
        # 规则路径：只返回证据，不编造答案（没有模型就没有答案可给）。
        # 这样「拿掉 key」时接口依然 200 + 有引用，且不会假装答了。
        answer = ''
        degraded = True
        degradation_reasons = ['未启用模型（use_agent=false 或未配置 API key），仅返回检索证据']

    # 🔴 计量：降级的问答**不扣额度**（没调模型 = 没消耗额度）
    usage = None
    quota = None
    quota_error = None
    try:
        from billing import quota_status, record_usage

        user = current_user()
        usage, quota_error = record_usage(
            tenant_id=tenant_id,
            kb_id=kb_id,
            question=question,
            tokens=len(answer) if answer else 0,
            degraded=degraded,
            user_id=user.id if user else None,
        )
        quota = quota_status(tenant_id)
    except QuotaExceeded as exc:
        # 🔴 额度用完 —— 上层转 403 + 明确提示，**不是 500**
        return None, exc
    except Exception as exc:      # 计量出错不该拖垮问答
        logger.warning('record_usage_failed', extra={
            'event': 'record_usage_failed',
            'kb_id': kb_id,
            'error': str(exc)[:200],
        })
        quota_error = str(exc)[:200]

    return {
        'answer': answer,
        'references': references,
        'citations': [str(r['document_id']) for r in references],
        'invalid_citations': [],
        'degraded': degraded,
        'degradation_reasons': degradation_reasons,
        'retrieved_count': len(hits),
        'evidence': evidence,
        'usage': {
            'billable': bool(usage) and not degraded,
            'degraded_no_charge': degraded,
            'quota': quota,
        },
        'usage_error': quota_error,
    }, None


_NO_KEY_ANSWER_MARKERS = (
    '服务未配置 API 密钥',
    '未配置 API 密钥',
    '暂时无法回答',
)


def _ask_via_agent(question, evidence):
    """经 agent 生成答案。失败一律降级，不抛。

    ✅ 主路径已修在源头（agent._empty_result 现在会如实标degraded=True），
    这里的文本匹配保留为**双保险**：万一将来 agent 又出现别的降级出口
    忘了标，外层还能兜住一层。

    之所以还要留：标记错了会直接影响阶段 4 的额度扣减
    （降级的问答不该扣额度），多一道检查成本很低。
    """

    try:
        import agent

        result = agent.run_agent(question)
        answer = result.get('answer', '')
        reasons = list(result.get('degradation_reasons') or [])

        if any(marker in answer for marker in _NO_KEY_ANSWER_MARKERS):
            reasons.append('未配置 API key，agent 走了规则降级路径')

        if not answer:
            return '', reasons or ['agent 返回空答案']
        if result.get('degraded') and not reasons:
            reasons = ['agent 内部走了降级路径']
        return answer, reasons
    except Exception as exc:
        logger.warning('ask_via_agent_failed', extra={
            'event': 'ask_via_agent_failed',
            'error': str(exc)[:200],
        })
        return '', [f'agent 调用失败: {str(exc)[:200]}']


def resolve_citations(answer, references):
    """把答案里的 [doc:N] 解析回**具体文档 + 段落**。

    返回列表：{document_id, title, chunk_index, citation}。
    解析不到的（引用了不存在的 doc）不出现在结果里 ——
    它们由 check_citation 的 invalid_citations 负责报警。
    """

    from skills.check_citation import extract_citation_ids

    index = {str(ref['document_id']): ref for ref in references}
    resolved = []
    for citation in extract_citation_ids(answer):
        ref = index.get(citation)
        if ref is not None:
            resolved.append({
                'citation': citation,
                'document_id': ref['document_id'],
                'title': ref['title'],
                'chunk_index': ref['chunk_index'],
            })
    return resolved


# ============ 序列化 ============
def _kb_json(kb):
    return {
        'id': kb.id,
        'name': kb.name,
        'description': kb.description,
        'created_at': kb.created_at.strftime('%Y-%m-%d %H:%M:%S') if kb.created_at else None,
    }


def _doc_json(doc):
    return {
        'id': doc.id,
        'kb_id': doc.kb_id,
        'title': doc.title,
        'chunk_index': doc.chunk_index,
        'content': doc.content,
        'created_at': doc.created_at.strftime('%Y-%m-%d %H:%M:%S') if doc.created_at else None,
    }


# ============ 路由注册 ============
def register_kb_routes(application):
    """把知识库路由挂到 app 上。"""

    @application.get('/api/kbs')
    @login_required('view_history')
    def api_list_kbs():
        return jsonify(list_kbs())

    @application.get('/api/kbs/<int:kb_id>')
    @login_required('view_history')
    def api_get_kb(kb_id):
        kb = get_kb(kb_id)
        if kb is None:
            return jsonify({'error': '知识库不存在'}), 404
        return jsonify(kb)

    @application.post('/api/kbs')
    @login_required('manage_kb')
    def api_create_kb():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': '缺少参数'}), 400

        kb, error = create_kb(payload)
        if error:
            return jsonify({'error': error}), 400
        return jsonify(kb), 201

    @application.delete('/api/kbs/<int:kb_id>')
    @login_required('manage_kb')
    def api_delete_kb(kb_id):
        if delete_kb(kb_id) is None:
            return jsonify({'error': '知识库不存在'}), 404
        return jsonify({'status': 'ok'})

    @application.get('/api/kbs/<int:kb_id>/documents')
    @login_required('view_history')
    def api_list_documents(kb_id):
        docs = list_documents(kb_id)
        if docs is None:
            return jsonify({'error': '知识库不存在'}), 404
        return jsonify(docs)

    @application.post('/api/kbs/<int:kb_id>/documents')
    @login_required('upload_doc')
    def api_upload_document(kb_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': '缺少参数'}), 400

        docs, error = upload_document(
            kb_id,
            payload.get('title'),
            payload.get('content'),
        )
        # 🔴 upload_document 用 (None, None) 表示「库不属于本租户」，
        # 与「参数不对」的 (None, error) 区分开。
        if docs is None and error is None:
            return jsonify({'error': '知识库不存在'}), 404
        if error:
            return jsonify({'error': error}), 400
        return jsonify({'chunks': docs}), 201

    @application.delete('/api/documents/<int:document_id>')
    @login_required('upload_doc')
    def api_delete_document(document_id):
        if delete_document(document_id) is None:
            return jsonify({'error': '文档不存在'}), 404
        return jsonify({'status': 'ok'})

    @application.post('/api/kbs/<int:kb_id>/ask')
    @login_required('ask')
    def api_ask_in_kb(kb_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': '缺少参数'}), 400

        result, error = ask_in_kb(
            kb_id,
            payload.get('question'),
            top_k=payload.get('top_k', 3) or 3,
            use_agent=bool(payload.get('use_agent')),
        )
        # 🔴 (None, None) = 库不属于本租户 → 404（不是 403）
        if result is None and error is None:
            return jsonify({'error': '知识库不存在'}), 404
        # 🔴 额度用完 → 403 + 明确提示，**不是 500**
        #    注意必须在通用 error 分支之前：QuotaExceeded 是异常对象，
        #    直接丢给 jsonify 会抛「not JSON serializable」。
        if isinstance(error, QuotaExceeded):
            return jsonify({
                'error': str(error),
                'code': 'quota_exceeded',
                'plan': error.plan,
                'quota_used': error.used,
                'quota_total': error.total,
            }), 403
        if error:
            return jsonify({'error': error}), 400

        # 把答案里的 [doc:N] 解析回具体文档与段落
        result['resolved_citations'] = resolve_citations(
            result.get('answer', ''), result.get('references', []),
        )
        return jsonify(result)

    return application
