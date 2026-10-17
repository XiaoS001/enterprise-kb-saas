#!/bin/sh
# RMB01 容器入口
#
# 🔴 核心职责（三条，都是踩过坑才加的）：
#
# 1. **调度器与 worker 的关系** —— 本项目没有后台调度器，
#    所以在多 worker 上没有 RMB03 那个「重复巡检」问题。
#    但 **SQLite + 多 worker 依然有问题**：多个进程同时写会撞
#    database is locked。计量扣额度是写操作，所以默认单 worker。
#
# 2. **数据目录可写性检查** —— 非 root 用户（appuser）如果拿不到
#    /app/data 的写权限，会等到第一次扣额度时才炸（那时用户已经在等了）。
#    宁可启动时就报错。
#
# 3. **拒绝明显错误的配置** —— 比如 MODEL 配了但没 key。
set -eu

WORKERS="${GUNICORN_WORKERS:-1}"
SCHEDULER_ON="${RMB01_SCHEDULER_ENABLED:-false}"

# --- 检查 1：SQLite 多 worker ---
if [ "$WORKERS" != "1" ]; then
    echo "⚠️  本项目默认使用 SQLite，多 worker 会并发写冲突（database is locked）" >&2
    echo "   计量扣额度是写操作，并发下会被 busy_timeout 拖慢甚至失败。" >&2
    echo "   如果确实要 $WORKERS 个 worker，请先把数据库换成 PostgreSQL：" >&2
    echo "     RMB01_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/rmb01" >&2
    echo "   详见 README「部署约束」一节。" >&2
    if [ "${RMB01_ALLOW_MULTI_WORKER:-false}" != "true" ]; then
        exit 1
    fi
    echo "   （已设置 RMB01_ALLOW_MULTI_WORKER=true，继续启动）" >&2
fi

# --- 检查 2：数据目录可写 ---
DATA_DIR="${RMB01_DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR" 2>/dev/null || true
if ! touch "$DATA_DIR/.write_test" 2>/dev/null; then
    echo "❌ 数据目录不可写：$DATA_DIR（当前用户 $(id -un)）" >&2
    echo "   容器以非 root 用户运行，挂载卷需要可写权限。" >&2
    echo "   修复：宿主目录 chmod 或改用命名卷（docker compose 默认用命名卷）。" >&2
    exit 1
fi
rm -f "$DATA_DIR/.write_test"

# --- 检查 3：配了模型但没 key ---
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    echo "⚠️  未配置 DEEPSEEK_API_KEY —— 问答会走降级路径（只返回检索证据）。" >&2
    echo "   这不影响启动，但演示/生产环境应配置。" >&2
fi

echo "▶ 启动 RMB01 · worker=$WORKERS · data=$DATA_DIR"

exec gunicorn \
    --bind "0.0.0.0:${RMB01_PORT:-5001}" \
    --workers "$WORKERS" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-60}" \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    "app:app"