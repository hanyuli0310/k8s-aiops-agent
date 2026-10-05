#!/usr/bin/env bash
# 停止全部四进程
#
# 只按 pid 文件精确停止，不用 pkill -f 模式匹配（踩坑记录见 scripts/README.md 坑 4）：
# 同一台机器上可能有队友或另一个会话在跑同名进程（uvicorn app.main:app /
# collector.main / vite），模式匹配会误杀别人的进程，甚至把执行命令的 ssh 会话
# 自己一起杀掉（命令行里含同样的字符串）。
#
# 用法：bash scripts/stop_all.sh
#      LOG_DIR=/root/logs bash scripts/stop_all.sh   # 需与启动时同一个 LOG_DIR
set -u

LOG_DIR="${LOG_DIR:-/tmp/aiops}"

for name in frontend backend data_collector mock_server; do
    pidfile="$LOG_DIR/$name.pid"
    if [[ ! -f $pidfile ]]; then
        echo "SKIP  $name 无 pid 文件（${pidfile}）"; continue
    fi
    pid=$(cat "$pidfile")
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "SKIP  $name 未在运行 (pid $pid)"; rm -f "$pidfile"; continue
    fi
    # 整组回收：start_all.sh 用 setsid 启动时 pgid == pid，
    # 这样 npm 的孙进程 vite 不会变成孤儿继续占着 5173。
    #
    # 但 macOS 没有 setsid，start_all.sh 会降级为普通后台启动 —— 那种情况下
    # pgid 是【执行脚本的 shell 的进程组】，按 pgid kill 会把自己的终端一起杀掉。
    # 所以只在 pgid == pid（确实独立成组）时才整组回收。
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [[ -n $pgid && $pgid == "$pid" ]]; then
        kill -TERM -"$pgid" 2>/dev/null
    fi
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then
        [[ -n $pgid && $pgid == "$pid" ]] && kill -9 -"$pgid" 2>/dev/null
        kill -9 "$pid" 2>/dev/null
        echo "STOP  $name (pid ${pid}，强制)"
    else
        echo "STOP  $name (pid $pid)"
    fi
    rm -f "$pidfile"
done

# 无 setsid 的平台（macOS）上 npm 的孙进程 vite 不会随父进程组一起走，
# 会继续占着 5173 导致下次启动静默退到 5174。按端口精确定位后单独回收。
if ! command -v setsid >/dev/null 2>&1; then
    for port in 5173 9001 8000; do
        leftover=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
        for lp in $leftover; do
            # 只回收本项目的进程，避免误杀同事或其他项目占用同端口的服务
            if ps -p "$lp" -o command= 2>/dev/null | grep -qE 'k8s-aiops-agent|vite|uvicorn app.main|collector.main'; then
                kill "$lp" 2>/dev/null && echo "STOP  端口 $port 残留进程 (pid $lp)"
            else
                echo "KEEP  端口 $port 被其他程序占用 (pid $lp)，未处理"
            fi
        done
    done
fi

echo "--- 端口占用（空=已全部释放）---"
if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -E ':(9001|8000|5173)' || true
else
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ':(9001|8000|5173)' || true
fi
