"""时序探针：注入 Redis 雪崩，逐拍观察「窗口内样本 → 规则命中」的过程。

为什么需要它：加了 CACHE-001/002 后第一次跑 L2，两条新规则和原本期望的
API-001 全都没报，但手查数据库时 1 分钟窗口内 CPU 80.8%、内存 86.8% 明明都过阈值。
"规则逻辑错"和"扫描时刻窗口里恰好没数据"这两种原因，看汇总结果是分不出来的。

用法：cd backend && RULE_WINDOW_MINUTES=1 .venv/bin/python -m eval._diag_cache_rule
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app import config, db                                        # noqa: E402
from app.rules import builtin                                     # noqa: E402

MOCK = "http://localhost:9001"
SCENARIO = "redis_cache_avalanche"


def _kv_window() -> dict:
    """窗口内 Redis 指标的样本数与均值 —— 与规则用的是同一个窗口起点。"""
    since = builtin._window_ms()
    out = {}
    for m in ("CpuUsage", "MemoryUsage"):
        r = db.fetch_one(
            "SELECT COUNT(*) n, AVG(avg) a FROM metrics "
            "WHERE namespace='acs_kvstore' AND metric_name=:m AND ts>=:w",
            {"m": m, "w": since})
        out[m] = (r["n"], round(r["a"], 1) if r["a"] is not None else None)
    return out


def _ingress_window() -> tuple:
    since = builtin._window_s()
    rows = db.fetch_all(
        "SELECT method, url, COUNT(*) c FROM ingress_logs WHERE ts>=:w GROUP BY method, url",
        {"w": since})
    return len(rows), sum(r["c"] for r in rows), builtin._min_samples()


def main():
    db.init_db()
    print(f"数据源 {config.DATA_SOURCE} / 规则窗口 {config.RULE_WINDOW_MINUTES} 分钟 / "
          f"接口最小样本 {builtin._min_samples()}")

    # 先清残留
    st = httpx.get(f"{MOCK}/control/world_status", timeout=10).json()
    for f in st.get("active_faults", []):
        httpx.post(f"{MOCK}/control/recover_fault",
                   json={"fault_id": f["fault_id"]}, timeout=15)
        print(f"清理残留故障 {f['fault_id']}")

    inj = httpx.post(f"{MOCK}/control/inject_fault",
                     json={"scenario_id": SCENARIO}, timeout=15).json()
    fid = inj.get("fault_id")
    print(f"已注入 {fid}，开始逐拍观察\n")
    print(f"{'秒':>4} {'kv样本(cpu/mem)':>18} {'cpu均值':>8} {'mem均值':>8} "
          f"{'接口数/样本':>12} {'命中规则'}")

    t0 = time.time()
    try:
        while time.time() - t0 < 240:
            kv = _kv_window()
            napi, nreq, minsmp = _ingress_window()
            hits = sorted({f["rule_id"] for f in
                           (builtin.check_cache_001() + builtin.check_cache_002()
                            + builtin.check_api_001() + builtin.check_db_001())})
            print(f"{time.time() - t0:4.0f} "
                  f"{str(kv['CpuUsage'][0]) + '/' + str(kv['MemoryUsage'][0]):>18} "
                  f"{str(kv['CpuUsage'][1]):>8} {str(kv['MemoryUsage'][1]):>8} "
                  f"{f'{napi}/{nreq}(≥{minsmp})':>12} {hits or '—'}")
            time.sleep(15)
    finally:
        httpx.post(f"{MOCK}/control/recover_fault", json={"fault_id": fid}, timeout=15)
        print(f"\n已恢复 {fid}")


if __name__ == "__main__":
    main()
