"""检索策略对比（维护项 A2）—— **所有数字都是现场跑出来的**。

## 为什么不做「向量 vs BM25」

任务单里 A2 的原话是对比「向量 vs BM25」。本机做不到诚实：
bge-small-zh-v1.5 要运行时从 HuggingFace 下载约 90MB，`RMB01_VECTOR_ENABLED`
默认 false。为了让页面有两组数就把向量列硬编一批「演示分数」，等于在
维护页面上造假 —— 那比不做这一项更糟。

## 现在比的是什么

同一份知识库、同一个问题，两种**真实可跑**的检索口径：

  · strict   —— 线上口径：停用词过滤 + 相对阈值 0.35 + 词面覆盖率
  · baseline —— 裸 BM25：分词不滤停用词、不看分数正负、不做覆盖率检查

关心的是同一件事：**这些过滤到底拦住了什么**。
baseline 永远能凑满 top_k 段，所以「裸 BM25 返回的段里有多少段其实
与问题无关」可以直接数出来（噪声段）—— 这就是不做过滤的代价。

负样本（库里压根没有的主题）是最能说明问题的一行：strict 返回 0 段，
baseline 照样返回 top_k 段，问答链路拿到这些段就会开始编。
"""
import time

import config
from kb_search import (
    passes_coverage,
    search_documents,
    search_documents_baseline,
    tokenize,
)


# 两种口径的展示名（前端不写死文案，改名只改这里）
STRATEGY_LABELS = {
    'strict': '线上口径',
    'baseline': '对照口径',
}

STRATEGY_HINTS = {
    'strict': '停用词过滤 + 相对阈值 0.35 + 词面覆盖率 ≥ 一半',
    'baseline': '裸 BM25：不分词过滤、不设阈值、不做覆盖率检查',
}


# 默认探针：覆盖演示语料里的几类主题，外加一条**负样本**。
# expected 为空且 expect_none=True 表示「这个问题库里没有答案，理想输出是 0 段」。
DEFAULT_PROBES = (
    {'question': '连接池大小怎么配', 'expected': '连接池'},
    {'question': '慢查询怎么排查', 'expected': '慢查询'},
    {'question': '缓存穿透怎么解决', 'expected': '缓存'},
    {'question': '额度怎么算', 'expected': '额度'},
    {'question': '密码怎么存的', 'expected': '密码'},
    {'question': '数据隔离是怎么做的', 'expected': '隔离'},
    {'question': '支持哪些文档格式', 'expected': '格式'},
    {'question': '公司年会在哪办', 'expect_none': True},
)

PREVIEW_CHARS = 140


def _preview(content):
    text = (content or '').replace('\n', ' ').strip()
    return text[:PREVIEW_CHARS] + ('…' if len(text) > PREVIEW_CHARS else '')


def _hits_json(hits):
    return [
        {
            'document_id': hit['document_id'],
            'title': hit['title'],
            'chunk_index': hit['chunk_index'],
            'score': round(float(hit.get('score') or 0), 4),
            'preview': _preview(hit['content']),
        }
        for hit in hits
    ]


def _judge(hits, expected, expect_none):
    """这一次检索算不算「答对了」。

    · 负样本（expect_none）：只有返回 0 段才算对
    · 有期望词：返回的段里要真的出现这个词
    · 没给期望词：有结果就算对（只看召回，不看相关）
    """

    if expect_none:
        return len(hits) == 0
    if not expected:
        return len(hits) > 0
    return any(expected in (f"{h['title']}{h['content']}") for h in hits)


def _timed(fn, *args, **kwargs):
    started = time.perf_counter()
    hits = fn(*args, **kwargs)
    return hits, round((time.perf_counter() - started) * 1000, 2)


def compare_question(kb_id, question, tenant_id, top_k=3,
                     expected=None, expect_none=False):
    """同一个问题，两种口径各跑一次。"""

    strict_hits, strict_ms = _timed(
        search_documents, kb_id, question, top_k=top_k, tenant_id=tenant_id,
    )
    baseline_hits, baseline_ms = _timed(
        search_documents_baseline, kb_id, question,
        top_k=top_k, tenant_id=tenant_id,
    )

    # 噪声：基线返回、但按线上口径根本过不了覆盖率检查的段
    strict_tokens = tokenize(question)
    noise = [
        hit for hit in baseline_hits
        if not passes_coverage(hit['content'], strict_tokens)
    ]

    strict_ids = {h['document_id'] for h in strict_hits}
    baseline_ids = {h['document_id'] for h in baseline_hits}

    return {
        'question': question,
        'expected': expected,
        'expect_none': expect_none,
        'strict': {
            'hits': _hits_json(strict_hits),
            'count': len(strict_hits),
            'ms': strict_ms,
            'hit': _judge(strict_hits, expected, expect_none),
        },
        'baseline': {
            'hits': _hits_json(baseline_hits),
            'count': len(baseline_hits),
            'ms': baseline_ms,
            'hit': _judge(baseline_hits, expected, expect_none),
            'noise_count': len(noise),
        },
        'overlap_count': len(strict_ids & baseline_ids),
    }


def _summarize(rows):
    def block(key):
        hits = sum(1 for r in rows if r[key]['hit'])
        return {
            'hit': hits,
            'hit_rate': round(hits / len(rows), 3) if rows else 0.0,
            'avg_ms': round(sum(r[key]['ms'] for r in rows) / len(rows), 2) if rows else 0.0,
            'avg_count': round(sum(r[key]['count'] for r in rows) / len(rows), 2) if rows else 0.0,
            'total_count': sum(r[key]['count'] for r in rows),
        }

    baseline_total = sum(r['baseline']['count'] for r in rows)
    baseline_noise = sum(r['baseline']['noise_count'] for r in rows)

    return {
        'probe_count': len(rows),
        'strict': block('strict'),
        'baseline': block('baseline'),
        'noise': {
            'count': baseline_noise,
            'total': baseline_total,
            'rate': round(baseline_noise / baseline_total, 3) if baseline_total else 0.0,
        },
    }


def compare_probes(kb_id, tenant_id, probes=None, top_k=3):
    """跑一组探针，返回逐条结果 + 汇总。"""

    probes = DEFAULT_PROBES if probes is None else probes

    # 预热：jieba 首次调用要加载词典，不预热会把第一条的耗时钟拉高，
    # 看起来像「这个问题特别慢」，其实只是加载。
    tokenize('预热分词器')

    rows = [
        compare_question(
            kb_id,
            probe['question'],
            tenant_id,
            top_k=top_k,
            expected=probe.get('expected'),
            expect_none=bool(probe.get('expect_none')),
        )
        for probe in probes
    ]
    return rows, _summarize(rows)


def vector_note():
    """向量路当前到底能不能跑 —— 如实返回，页面据此显示或隐藏。"""

    enabled = bool(config.VECTOR_ENABLED)
    return {
        'enabled': enabled,
        'model': config.EMBEDDING_MODEL,
        'reason': '' if enabled else (
            '向量路未启用：RMB01_VECTOR_ENABLED=false。'
            '嵌入模型需运行时从 HuggingFace 下载约 90MB，'
            '离线环境首次运行会失败，所以默认关闭，'
            '本页也不拿假的向量分数凑一列。'
        ),
    }


def register_compare_routes(app):
    """挂检索对比路由（只读，按租户隔离）。"""

    from flask import jsonify, request

    from auth import current_tenant_id, fetch_scoped, login_required
    from models import Kb

    @app.post('/api/compare/retrieval')
    @login_required('view_history')
    def api_compare_retrieval():
        payload = request.get_json(silent=True) or {}

        tenant_id = current_tenant_id()
        if tenant_id is None:
            return jsonify({'error': '平台管理员没有自己的知识库'}), 403

        kb_id = payload.get('kb_id')
        if kb_id in (None, ''):
            first = Kb.query.filter_by(tenant_id=tenant_id).order_by(Kb.id).first()
            if first is None:
                return jsonify({'error': '当前租户还没有知识库'}), 404
            kb_id = first.id
        else:
            try:
                kb_id = int(kb_id)
            except (TypeError, ValueError):
                return jsonify({'error': 'kb_id 无效'}), 400

        # 🔴 跨租户取不到 → 404（不返 403，403 会泄漏这个库存在）
        kb = fetch_scoped(Kb, kb_id)
        if kb is None:
            return jsonify({'error': '知识库不存在'}), 404

        try:
            top_k = int(payload.get('top_k') or 3)
        except (TypeError, ValueError):
            top_k = 3
        top_k = max(1, min(top_k, 10))

        question = (payload.get('question') or '').strip()
        if question and len(question) > 500:
            return jsonify({'error': '问题过长'}), 400

        if question:
            rows = [compare_question(kb_id, question, tenant_id, top_k=top_k)]
            summary = _summarize(rows)
            mode = 'question'
        else:
            rows, summary = compare_probes(kb_id, tenant_id, top_k=top_k)
            mode = 'probes'

        return jsonify({
            'mode': mode,
            'kb': {'id': kb.id, 'name': kb.name},
            'top_k': top_k,
            'strategies': {
                key: {'name': STRATEGY_LABELS[key], 'hint': STRATEGY_HINTS[key]}
                for key in STRATEGY_LABELS
            },
            'rows': rows,
            'summary': summary,
            'vector': vector_note(),
        })

    return app
