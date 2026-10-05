"""实测验证：upgrade_rds_instance 作用在**缓存实例**上到底改变了什么。

为什么单独测：给 upgrade_rds_instance 的工具描述里写了「只能降低内存与连接使用率，
对 CPU 高水位无效」—— 这句话原本只是我读 mock_server/app/actions.py 推断出来的
（set_effect 只碰 conn_pct 和 mem_pct）。工具描述会直接进模型的系统提示词，
写一句没验证过的断言，等于把我的猜测当事实喂给模型。所以实测一遍。

⚠️ 第一版这个脚本本身就给出了错结论：它把动作发往 `/control/apply_action`，
而真实端点是 `/control/actions` —— 服务端返回 404，而脚本没检查状态码，
于是“升配后三项水位都没变”被当成了结论，实际上测的是“什么都没做”。
教训：验证类脚本必须 `raise_for_status()`，否则它会把自己的 bug 报告成被测系统的行为。

用法（务必在没有其它评测在跑的时候执行，会注入并恢复故障）：
    cd backend && .venv/bin/python -m eval._diag_upgrade_on_cache
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

MOCK = "http://localhost:9001"
INST = "kvstore-redis-01"


def _redis() -> dict:
    d = httpx.get(f"{MOCK}/control/world_status", timeout=10).json()
    for i in d.get("instances", []):
        if i.get("instance") == INST:
            return {k: i.get(k) for k in ("cpu_pct", "mem_pct", "conn_pct")}
    return {}


def _show(tag: str):
    r = _redis()
    print(f"  {tag:22s} cpu={r.get('cpu_pct')}  mem={r.get('mem_pct')}  conn={r.get('conn_pct')}")
    return r


def _act(action_type: str, target: str) -> dict:
    """发一个世界动作。**必须看返回体**：mock 端对不适用的 target 会返回
    {"accepted": False, "error": ...} 而不是 HTTP 错误，静默忽略它同样会得出假结论。
    """
    r = httpx.post(f"{MOCK}/control/actions",
                   json={"action_type": action_type, "target": target,
                         "params": {}, "source": "eval-diag"}, timeout=15)
    r.raise_for_status()
    body = r.json()
    print(f"  动作 {action_type} → {target}: {body}")
    return body


def main():
    d = httpx.get(f"{MOCK}/control/world_status", timeout=10).json()
    if d.get("active_faults"):
        raise SystemExit(f"❌ 世界里有活跃故障 {[f['fault_id'] for f in d['active_faults']]}，"
                         f"先恢复再测（否则读到的是叠加态）")

    base = _show("稳态")
    inj = httpx.post(f"{MOCK}/control/inject_fault",
                     json={"scenario_id": "redis_cache_avalanche"}, timeout=15).json()
    fid = inj["fault_id"]
    time.sleep(20)
    before = _show("雪崩后")

    # 场景定义里写的 recover_actions 是 [upgrade_rds, scale_out]，两个都试
    for a in ("upgrade_rds", "scale_out"):
        _act(a, INST)
    for s in (15, 30, 45):
        time.sleep(15)
        after = _show(f"动作后 {s}s")

    print("\n结论：")
    for k, label in (("mem_pct", "内存"), ("conn_pct", "连接"), ("cpu_pct", "CPU")):
        b, a = before.get(k), after.get(k)
        if b and a:
            print(f"  {label}: {b} → {a}（{'下降' if a < b * 0.95 else '基本未变'}），"
                  f"稳态参考 {base.get(k)}")

    httpx.post(f"{MOCK}/control/recover_fault", json={"fault_id": fid}, timeout=15)
    print(f"\n已恢复 {fid}")


if __name__ == "__main__":
    main()
