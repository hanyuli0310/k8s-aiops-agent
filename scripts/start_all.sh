#!/usr/bin/env bash
# 一键启动全部四进程（live 模式）
#
# 启动顺序：mock_server:9001 -> data_collector -> backend:8000 -> frontend:5173
# 乱序也能最终一致，但按序启动首轮数据更快就绪。
#
# 用法：
#   bash scripts/start_all.sh                          # 前端对外开放（0.0.0.0:5173）
#   FRONTEND_HOST=127.0.0.1 bash scripts/start_all.sh  # 前端只监听本机（配 SSH 隧道用）
#   LOG_DIR=/root/logs bash scripts/start_all.sh       # 自定义日志与 pid 目录
#
# 网络暴露约定（踩坑记录见 scripts/README.md 坑 1）：
#   mock_server 与 backend 始终只监听 127.0.0.1。外部对 /api 的请求由 vite 在机器
#   内部代理到 127.0.0.1:8000，所以对外只需放行 5173 一个端口——backend 的 LLM
#   调用入口和 mock 的 /control/inject_fault 控制面都不会暴露到公网。
#
# 重复执行安全：已在运行的服务会跳过，可用来补起单个挂掉的进程。
# 停止：bash scripts/stop_all.sh（同一个 LOG_DIR）
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp/aiops}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
PY="$ROOT/backend/.venv/bin/python"
UVICORN="$ROOT/backend/.venv/bin/uvicorn"

if [[ ! -x $UVICORN ]]; then
    echo "ERROR 未找到 ${UVICORN}，请先创建虚拟环境并安装依赖："
    echo "  cd $ROOT/backend && python3 -m venv .venv \\"
    echo "    && .venv/bin/pip install -r requirements.txt \\"
    echo "       -r ../mock_server/requirements.txt -r ../data_collector/requirements.txt"
    exit 1
fi

mkdir -p "$LOG_DIR"
[[ -f $ROOT/mock_server/.env ]]    || cp "$ROOT/mock_server/.env.example"    "$ROOT/mock_server/.env"
[[ -f $ROOT/data_collector/.env ]] || cp "$ROOT/data_collector/.env.example" "$ROOT/data_collector/.env"

start() {  # start <name> <workdir> <cmd...>
    local name=$1 wd=$2; shift 2
    local pidfile="$LOG_DIR/$name.pid"
    if [[ -f $pidfile ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "SKIP  $name 已在运行 (pid $(cat "$pidfile"))"; return 0
    fi
    cd "$wd" || return 1
    # setsid：每个服务独立成会话/进程组，停止时可整组回收。否则 npm -> vite 的孙进程
    # 会在父进程被杀后变成孤儿继续占着 5173，新实例静默退到 5174
    # （日志里只有一句 "Port 5173 is in use, trying another one..."）。
    #
    # setsid 是 util-linux 专有命令，**macOS 上不存在**：此前脚本只在 ECS 验证过，
    # 在 Mac 上四个服务会全部以 "setsid: command not found" 秒退，
    # 而 pid 文件已写入，看起来像"启动成功"。所以这里必须检测后降级。
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$@" > "$LOG_DIR/$name.log" 2>&1 &
    else
        # 无 setsid：进程组与父 shell 相同，不能按 pgid 整组 kill（会连带杀掉
        # 执行脚本的 shell）。stop_all.sh 已按平台区分处理。
        nohup "$@" > "$LOG_DIR/$name.log" 2>&1 &
    fi
    echo $! > "$pidfile"
    echo "START $name pid=$! -> $LOG_DIR/$name.log"
}

echo "[1/4] mock_server :9001"
start mock_server    "$ROOT/mock_server"    "$UVICORN" app.main:app --host 127.0.0.1 --port 9001
sleep 6
echo "[2/4] data_collector"
start data_collector "$ROOT/data_collector" "$PY" -m collector.main
sleep 3
echo "[3/4] backend :8000 (DATA_SOURCE=live)"
start backend        "$ROOT/backend"        env DATA_SOURCE=live "$UVICORN" app.main:app --host 127.0.0.1 --port 8000
sleep 8
echo "[4/4] frontend :5173 (host=$FRONTEND_HOST)"
start frontend       "$ROOT/frontend"       npm run dev -- --host "$FRONTEND_HOST"

sleep 6
echo "--- 健康检查 ---"
curl -s -m 5 127.0.0.1:9001/health && echo
curl -s -m 5 127.0.0.1:8000/api/status | head -c 160 && echo
curl -s -m 5 -o /dev/null -w "frontend HTTP %{http_code}\n" 127.0.0.1:5173
echo "--- 监听端口 ---"
ss -ltnp 2>/dev/null | grep -E ':(9001|8000|5173)' || echo "（端口尚未就绪，稍等后再查）"
if [[ $FRONTEND_HOST == 0.0.0.0 ]]; then
    echo "完成。浏览器访问 http://<服务器IP>:5173（需安全组放行 5173，演示后记得删规则）"
else
    echo "完成。前端只监听 ${FRONTEND_HOST}，从本机访问需先建 SSH 隧道（见 scripts/README.md 坑 3）"
fi
