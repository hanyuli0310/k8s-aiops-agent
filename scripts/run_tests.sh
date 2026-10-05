#!/usr/bin/env bash
# 全量测试入口（CI 的执行体）。一条命令跑完后端 + mock + collector + 前端校验。
#
# 设计要点：
# 1. 自己定位仓库根与 .venv，不依赖调用方的 cwd 与激活状态 —— CI runner 里没有
#    人给你 source activate。
# 2. 跑之前先做【数据库隔离自检】：确认每个测试文件都会连本地 SQLite。
#    曾发生过测试连到线上 RDS、DELETE 打在真实库上的事故（见 CLAUDE.md §8.6），
#    这道自检是那次事故的机制性防护，不要删。
# 3. 失败时打印该项的最后 20 行输出，而不是只给一个非零退出码。
#
# 用法：
#   bash scripts/run_tests.sh              # 全量
#   bash scripts/run_tests.sh --no-frontend  # 只跑 Python（无 node 环境时）
#   bash scripts/run_tests.sh --backend-only
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
OUT_DIR="$(mktemp -d)"
trap 'rm -rf "$OUT_DIR"' EXIT

RUN_FRONTEND=1
RUN_SIDECARS=1
for arg in "$@"; do
    case "$arg" in
        --no-frontend)   RUN_FRONTEND=0 ;;
        --backend-only)  RUN_FRONTEND=0; RUN_SIDECARS=0 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数: ${arg}（用 --help 看用法）"; exit 2 ;;
    esac
done

if [[ ! -x $PY ]]; then
    echo "❌ 找不到虚拟环境解释器：$PY"
    echo "   先建环境：cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 2
fi

PASS_TOTAL=0
FAIL_TOTAL=0
FAILED_ITEMS=()
RESULTS=()

# ─────────────────────────────────────────────
# 数据库隔离自检
# ─────────────────────────────────────────────
# 每个【会连数据库】的测试文件都必须在导入业务模块之前把连接串指向本地 SQLite，
# 并自带运行时闸门。这里只做静态检查：文件里必须同时出现 sqlite 连接串与闸门断言。
#
# 判定"是否会连数据库"的依据是它有没有 import db 模块 —— mock_server 是纯内存
# 世界模拟器，压根没有数据库概念，对它要求隔离是噪声。
#
# 静态检查抓不到"运行时又被 .env 覆盖"的情况，所以测试文件内部的运行时闸门
# 仍然是主防线 —— 两道都要有。
check_isolation() {
    local bad=0 checked=0 skipped=0
    echo "── 数据库隔离自检 ──"
    while IFS= read -r f; do
        local base
        base="$(basename "$f")"
        if ! grep -qE '^from (app|collector) import .*\bdb\b|^from \.\.? import .*\bdb\b|^import .*\bdb$' "$f"; then
            # ${base} 必须带花括号：紧跟中文全角括号时，bash 会把非 ASCII 字节
            # 当成变量名的一部分，报 "base（...: unbound variable"。
            # 同类坑在 start_all.sh 的 $FRONTEND_HOST，已经踩过一次。
            echo "  – ${base}（不涉及数据库，跳过）"
            skipped=$((skipped + 1))
            continue
        fi
        checked=$((checked + 1))
        if ! grep -q 'sqlite:///' "$f"; then
            echo "  ✗ $base 没有把连接串指向 sqlite"
            bad=1
            continue
        fi
        if ! grep -qE 'startswith\("sqlite"\)|startswith\(.sqlite.\)' "$f"; then
            echo "  ✗ $base 缺少运行时隔离闸门断言"
            bad=1
            continue
        fi
        echo "  ✓ $base"
    done < <(find "$ROOT/backend/tests" "$ROOT/mock_server/tests" "$ROOT/data_collector/tests" \
                  -name 'test_*.py' 2>/dev/null | sort)
    if [[ $bad -ne 0 ]]; then
        echo
        echo "❌ 隔离自检未通过，已中止 —— 测试可能连到真实数据库。"
        echo "   修法：在导入业务模块之前设好 DATABASE_URL 与 DB_URL 指向临时 sqlite，"
        echo "   并在导入后断言连接串以 sqlite 开头（参照 tests/test_skill_accuracy.py）。"
        exit 3
    fi
    echo "  ${checked} 个涉库测试全部隔离，${skipped} 个无关文件跳过"
    echo
}

# ─────────────────────────────────────────────
# 跑一个 Python 测试文件
# ─────────────────────────────────────────────
# $1=显示名  $2=工作目录  $3=测试文件相对路径
#
# 退出码只用来区分"跑完了但有失败"与"根本没跑起来"。
# 判定顺序必须是【先解析日志、再看退出码】：测试套件失败时会 sys.exit(1)，
# 若按退出码优先就会把正常的用例失败报成 CRASH，汇总里还会漏掉失败计数，
# 出现"0 失败 / 但有失败项"的自相矛盾输出。
run_py_suite() {
    local name="$1" workdir="$2" rel="$3"
    local log="$OUT_DIR/${name//\//_}.log"
    local code=0
    printf '%-34s' "$name"
    (cd "$workdir" && "$PY" "$rel" > "$log" 2>&1) || code=$?

    local p f
    p=$(grep -oE '^通过 [0-9]+' "$log" | tail -1 | grep -oE '[0-9]+' || true)
    f=$(grep -oE '失败 [0-9]+$' "$log" | tail -1 | grep -oE '[0-9]+' || true)

    if [[ -z ${p:-} && -z ${f:-} ]]; then
        # 连汇总行都没有 → 导入期就崩了，或被隔离闸门拦下（SystemExit 2/3）
        echo "❌ 未跑起来（exit=${code}）"
        FAILED_ITEMS+=("$name")
        RESULTS+=("$name|CRASH|0|0")
        tail -20 "$log" | sed 's/^/      /'
        return
    fi

    p=${p:-0}; f=${f:-0}
    PASS_TOTAL=$((PASS_TOTAL + p))
    FAIL_TOTAL=$((FAIL_TOTAL + f))
    if [[ $f -gt 0 || $code -ne 0 ]]; then
        echo "❌ $p 通过 / $f 失败（exit=${code}）"
        FAILED_ITEMS+=("$name")
        RESULTS+=("$name|FAIL|$p|$f")
        grep '✗' "$log" | head -10 | sed 's/^/      /'
    else
        echo "✅ $p 项"
        RESULTS+=("$name|OK|$p|0")
    fi
}

# ─────────────────────────────────────────────
# 前端校验
# ─────────────────────────────────────────────
run_frontend() {
    if ! command -v npx >/dev/null 2>&1; then
        echo "── 前端校验：跳过（未找到 npx）──"
        RESULTS+=("frontend|SKIP|0|0")
        return
    fi
    if [[ ! -d $ROOT/frontend/node_modules ]]; then
        echo "── 前端校验：跳过（node_modules 未安装，先 cd frontend && npm install）──"
        RESULTS+=("frontend|SKIP|0|0")
        return
    fi
    for step in "tsc --noEmit:npx tsc --noEmit" "build:npm run build"; do
        local label="${step%%:*}" cmd="${step#*:}"
        local log="$OUT_DIR/frontend_${label// /_}.log"
        printf '%-34s' "frontend · $label"
        if (cd "$ROOT/frontend" && eval "$cmd" > "$log" 2>&1); then
            echo "✅"
            RESULTS+=("frontend $label|OK|0|0")
        else
            echo "❌"
            FAILED_ITEMS+=("frontend $label")
            RESULTS+=("frontend $label|FAIL|0|0")
            tail -20 "$log" | sed 's/^/      /'
        fi
    done
}

# ═════════════════════════════════════════════
echo "仓库：$ROOT"
echo "解释器：$PY"
echo
check_isolation

echo "── backend ──"
for f in "$ROOT"/backend/tests/test_*.py; do
    run_py_suite "$(basename "$f")" "$ROOT/backend" "tests/$(basename "$f")"
done

if [[ $RUN_SIDECARS -eq 1 ]]; then
    echo
    echo "── mock_server / data_collector ──"
    [[ -f $ROOT/mock_server/tests/test_mock_server.py ]] && \
        run_py_suite "test_mock_server.py" "$ROOT/mock_server" "tests/test_mock_server.py"
    [[ -f $ROOT/data_collector/tests/test_collector.py ]] && \
        run_py_suite "test_collector.py" "$ROOT/data_collector" "tests/test_collector.py"
fi

if [[ $RUN_FRONTEND -eq 1 ]]; then
    echo
    echo "── frontend ──"
    run_frontend
fi

echo
echo "══════════════════════════════════════════"
printf '用例合计：%d 通过 / %d 失败\n' "$PASS_TOTAL" "$FAIL_TOTAL"
if [[ ${#FAILED_ITEMS[@]} -gt 0 ]]; then
    echo "失败项：${FAILED_ITEMS[*]}"
    echo "══════════════════════════════════════════"
    exit 1
fi
echo "全部通过 ✅"
echo "══════════════════════════════════════════"
