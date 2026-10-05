"""mock_server 关键路径测试（此前该模块零测试）。

覆盖：世界演进 tick、故障注入/恢复、治理动作生效、渲染器契约、**并发安全**。

零依赖，直接运行：
    cd mock_server && ../backend/.venv/bin/python tests/test_mock_server.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, world_def as W                    # noqa: E402
from app import actions, faults                           # noqa: E402
from app.engine import WorldEngine                        # noqa: E402
from app.renderers import cms, sls                        # noqa: E402


def _fresh(ticks: int = 5) -> WorldEngine:
    """每个用例用独立引擎，避免相互污染（引擎是有状态的）。"""
    eng = WorldEngine(seed=42)
    for _ in range(ticks):
        eng.tick()
    return eng


# ══════════════════════════════════════════════════════
# 世界演进
# ══════════════════════════════════════════════════════

def test_world_self_check():
    """世界观自检必须通过，否则引擎拒绝启动。"""
    check = W.self_check()
    assert check["ok"], check.get("errors")
    print(f"  ✓ 世界观自检通过：{check['summary']}")


def test_tick_advances_and_generates_data():
    eng = _fresh(0)
    assert eng.tick_no == 0
    stats = eng.tick()
    assert eng.tick_no == 1
    assert stats["requests"] > 0, "一拍应产生流量"
    assert len(eng.trace_buffer) > 0, "应采样出 trace"
    assert len(eng.log_buffer["nginx-ingress"]) > 0, "应产生 ingress 日志"
    print(f"  ✓ 一拍产生 {stats['requests']} 请求 / "
          f"{stats['sampled_traces']} trace / {len(eng.log_buffer['nginx-ingress'])} 日志")


def test_deterministic_with_same_seed():
    """同 seed 应可复现 —— 否则演示不可控、问题无法重放。"""
    a, b = WorldEngine(seed=7), WorldEngine(seed=7)
    sa = [a.tick()["requests"] for _ in range(5)]
    sb = [b.tick()["requests"] for _ in range(5)]
    assert sa == sb, f"同 seed 结果不同：{sa} vs {sb}"
    print(f"  ✓ 同 seed 可复现：{sa}")


def test_buffers_bounded():
    """长跑不能让 buffer 无限膨胀。"""
    eng = WorldEngine(seed=1)
    for _ in range(40):
        eng.tick()
    assert eng.trace_buffer.maxlen is not None, "trace_buffer 应有 maxlen"
    for name, buf in eng.log_buffer.items():
        assert buf.maxlen is not None, f"{name} 应有 maxlen"
    # metric series 数量应有上界（来自固定实例集 × 固定指标名）
    n1 = len(eng.metric_buffer)
    for _ in range(20):
        eng.tick()
    assert len(eng.metric_buffer) == n1, \
        f"metric series 数量增长了 {n1} → {len(eng.metric_buffer)}（应由固定实例集封顶）"
    print(f"  ✓ buffer 均有上界；metric series 稳定在 {n1} 个")


# ══════════════════════════════════════════════════════
# 故障注入与恢复
# ══════════════════════════════════════════════════════

def test_inject_and_recover_roundtrip():
    eng = _fresh()
    sid = "rds_conn_spike"
    out = faults.inject(eng, sid)
    assert "error" not in out, out
    fid = out["fault_id"]
    assert any(f["fault_id"] == fid for f in eng.active_faults), "故障未登记"
    assert any(e.startswith(f"fault:{fid}")
               for e in {v.get("source", "") for d in eng.effects.values()
                         for v in d.values()}), "未产生带 fault_id 标记的 effect"

    rec = faults.recover(eng, fid)
    assert "error" not in rec, rec
    assert not any(f["fault_id"] == fid and f.get("status") == "active"
                   for f in eng.active_faults), "恢复后故障仍是 active"
    print(f"  ✓ {sid} 注入 {fid} → 恢复闭环完整")


def test_recover_isolates_by_fault_id():
    """★ 恢复 A 不能清掉 B 的 effect（两个故障影响同一实体时）。"""
    eng = _fresh()
    a = faults.inject(eng, "rds_conn_spike")
    b = faults.inject(eng, "slow_query_storm")
    assert "error" not in a and "error" not in b

    def sources():
        return {v.get("source") for d in eng.effects.values() for v in d.values()}

    before = sources()
    assert f"fault:{a['fault_id']}" in before and f"fault:{b['fault_id']}" in before

    faults.recover(eng, a["fault_id"])
    after = sources()
    assert f"fault:{b['fault_id']}" in after, "恢复 A 时误清了 B 的 effect"
    print("  ✓ effect 按 fault_id 隔离，恢复 A 不影响 B")


def test_unknown_scenario_rejected():
    eng = _fresh(1)
    out = faults.inject(eng, "no_such_scenario")
    assert "error" in out and "unknown" in out["error"], out
    print("  ✓ 未知场景被拒")


def test_every_scenario_actually_moves_the_world():
    """★★ 每个场景注入后都必须真的改变可观测结果，否则是空壳场景。

    为什么需要这条：场景的数值是照着稳态基线手算的，很容易算错 ——
    比如给 nginx-ingress 加时延效应，但它不是任何接口的 backend，
    注入了却什么也不会发生。这种"看起来有 12 个场景、实际只有 4 个有效"
    的情况，会让基于场景的量化评测直接失去意义（真值有，现象没有）。

    负样本（expect_no_new_findings）反过来判：它【不该】造成越阈。
    """
    for sid, sc in faults.SCENARIOS.items():
        eng = _fresh(5)
        base_slow = sum(1 for a in eng.apis.values() if a["p99_ms"] > 1000)
        base_err = sum(1 for a in eng.apis.values() if a["error_rate"] > 0.01)
        faults.inject(eng, sid)
        for _ in range(5):
            eng.tick()
        slow = sum(1 for a in eng.apis.values() if a["p99_ms"] > 1000)
        errs = sum(1 for a in eng.apis.values() if a["error_rate"] > 0.01)

        if sc.get("expect_no_new_findings"):
            assert slow == base_slow and errs == base_err, (
                f"{sid} 是负样本，却把接口指标推过了阈值："
                f"P99>1s {base_slow}→{slow}、err>1% {base_err}→{errs}")
        else:
            assert (slow > base_slow) or (errs > base_err), (
                f"{sid} 注入后接口指标毫无变化（P99>1s {slow}、err>1% {errs}），"
                f"场景是空壳 —— 检查 target 是否真的在链路上、效应 key 是否被引擎支持")
    print(f"  ✓ {len(faults.SCENARIOS)} 个场景注入后均产生预期的可观测变化"
          f"（负样本确认不越阈）")


def test_confusable_pairs_are_symmetric_and_distinct():
    """★ 易混淆对必须互相指认、且根因实体确实不同。

    这组标注是评测"能否区分表层现象与真实根因"的依据。如果标反了或
    指向自己，那项评测就测不到东西了。
    """
    pairs = {sid: sc["confusable_with"] for sid, sc in faults.SCENARIOS.items()
             if sc.get("confusable_with")}
    assert pairs, "没有任何易混淆对，无法评测根因区分能力"
    for sid, other in pairs.items():
        assert other in faults.SCENARIOS, f"{sid} 指向不存在的场景 {other}"
        assert other != sid, f"{sid} 指向了自己"
        a, b = faults.SCENARIOS[sid], faults.SCENARIOS[other]
        assert a["target"] != b["target"] or a.get("expected_table") != b.get("expected_table"), (
            f"{sid} 与 {other} 的根因完全相同，不构成易混淆对")
    print(f"  ✓ {len(pairs)} 组易混淆标注有效，根因实体/表均不相同")


def test_scenario_metadata_complete():
    """场景元数据要完整：前端要展示、Skill 要引用 expected_rules。"""
    for sid, sc in faults.SCENARIOS.items():
        for field in ("title", "target", "description", "recover_actions"):
            assert sc.get(field), f"{sid} 缺字段 {field}"
        # expected_rules 允许为空列表，但【必须】配合 expect_no_new_findings 显式声明
        # ——否则"漏写真值"和"故意的负样本"就分不开，评测会拿错真值判对错。
        assert "expected_rules" in sc, f"{sid} 缺字段 expected_rules"
        if not sc["expected_rules"]:
            assert sc.get("expect_no_new_findings"), (
                f"{sid} 的 expected_rules 为空却没标 expect_no_new_findings，"
                f"无法区分是负样本还是漏写")
        assert sc["recover_actions"], f"{sid} 没有可用的恢复动作"
        for act in sc["recover_actions"]:
            assert act in actions.ACTION_TYPES, \
                f"{sid} 的恢复动作 {act} 不在 ACTION_TYPES 里（前端点了会失败）"
    print(f"  ✓ {len(faults.SCENARIOS)} 个场景元数据完整，恢复动作均可执行")


# ══════════════════════════════════════════════════════
# 治理动作
# ══════════════════════════════════════════════════════

def test_scale_out_changes_replicas():
    eng = _fresh()
    svc = "order-service"
    before = eng.service_state[svc]["replicas"]
    out = actions.apply_action(eng, "scale_out", svc, {"replicas": before + 2})
    assert out.get("accepted"), out
    assert eng.service_state[svc]["replicas"] == before + 2, \
        f"副本数未变：{before} → {eng.service_state[svc]['replicas']}"
    print(f"  ✓ scale_out 生效：{svc} {before} → {before + 2} 副本")


def test_patch_resources_affects_oversale():
    """下调 CPU limit 应真的降低超卖率（治理闭环的核心因果）。"""
    eng = _fresh()
    before = eng.current_oversale_pct()
    svc = next(s for s in eng.service_state if W.SERVICES[s]["namespace"] == "default")
    out = actions.apply_action(eng, "patch_resources", svc,
                              {"cpu_limit": "100m"})
    assert out.get("accepted"), out
    after = eng.current_oversale_pct()
    assert after < before, f"超卖率未下降：{before:.2f}% → {after:.2f}%"
    print(f"  ✓ 下调 {svc} CPU limit：超卖率 {before:.1f}% → {after:.1f}%")


def test_unknown_action_rejected():
    eng = _fresh(1)
    out = actions.apply_action(eng, "rm_rf", "order-service")
    assert out.get("accepted") is False and "unknown action_type" in out.get("error", "")
    print("  ✓ 未知动作被拒（不会静默当成功）")


def test_action_recorded_in_recent():
    eng = _fresh()
    actions.apply_action(eng, "restart_pod", "order-service")
    assert eng.recent_actions, "动作未记入 recent_actions（前端控制台读它）"
    print(f"  ✓ 动作已记录：{eng.recent_actions[-1].get('action_type')}")


# ══════════════════════════════════════════════════════
# 渲染器契约（backend 的 ingest 依赖这些格式）
# ══════════════════════════════════════════════════════

def test_cms_datapoints_is_json_string():
    """格式坑：Datapoints 必须是 JSON 字符串，消费方要二次 loads。"""
    eng = _fresh(15)          # 跑够拍数以跨过分钟边界，产生 CMS 点
    metrics = cms.list_metrics(eng)
    assert metrics, "无可用指标"
    m = metrics[0]
    out = cms.describe_metric_list(eng, m["Namespace"], m["MetricName"])
    assert isinstance(out["Datapoints"], str), "Datapoints 应为 JSON 字符串"
    pts = json.loads(out["Datapoints"])
    assert isinstance(pts, list)
    if pts:
        assert "timestamp" in pts[0] and "Average" in pts[0], pts[0]
    print(f"  ✓ CMS 契约正确：{m['MetricName']} → {len(pts)} 个数据点（JSON 字符串）")


def test_sls_trace_and_log_formats():
    eng = _fresh(5)
    tr = sls.get_logs(eng, "trace", 0, int(time.time()) + 60)
    assert tr["logs"], "trace logstore 为空"
    span = tr["logs"][0]
    assert isinstance(span["attribute"], str), "trace 的 attribute 应为 JSON 字符串"
    assert isinstance(span["__time__"], int), "trace 的 __time__ 应为秒级整数"

    lg = sls.get_logs(eng, "nginx-ingress", 0, int(time.time()) + 60)
    assert lg["logs"], "ingress logstore 为空"
    assert all(isinstance(v, str) for v in lg["logs"][0].values()), \
        "日志类 logstore 的值应全部是字符串"
    print(f"  ✓ SLS 契约正确：trace {tr['total']} 条（attribute 为 JSON 串）、"
          f"ingress {lg['total']} 条（全字符串）")


def test_sls_unknown_logstore_and_paging():
    eng = _fresh(3)
    bad = sls.get_logs(eng, "nope")
    assert "error" in bad and bad["count"] == 0 and "available" in bad

    p1 = sls.get_logs(eng, "nginx-ingress", 0, int(time.time()) + 60, offset=0, lines=5)
    p2 = sls.get_logs(eng, "nginx-ingress", 0, int(time.time()) + 60, offset=5, lines=5)
    assert p1["count"] <= 5 and p2["count"] <= 5
    assert p1["total"] == p2["total"], "分页时 total 应一致"
    if p1["logs"] and p2["logs"]:
        assert p1["logs"][0] != p2["logs"][0], "分页未真正偏移"
    print(f"  ✓ 未知 logstore 有明确报错；分页正确（total={p1['total']}）")


# ══════════════════════════════════════════════════════
# 并发安全（本次修复的核心）
# ══════════════════════════════════════════════════════

def test_no_race_between_tick_and_renderers():
    """★ tick 写 buffer 与渲染器读 buffer 并发时不得抛异常。

    修复前实测：8 秒内 39 次 RuntimeError('deque mutated during iteration')，
    栈指向 renderers/sls.py 的列表推导。tick 在线程池里 popleft，
    渲染器在别的线程遍历同一个 deque。
    """
    eng = _fresh(20)
    errors, stop = [], threading.Event()

    def ticker():
        while not stop.is_set():
            try:
                eng.tick()
            except Exception as e:              # noqa: BLE001
                errors.append(("tick", repr(e)))

    def reader(name, fn):
        while not stop.is_set():
            try:
                fn()
            except Exception as e:              # noqa: BLE001
                errors.append((name, repr(e)))

    now = lambda: int(time.time()) + 60         # noqa: E731
    ths = [
        threading.Thread(target=ticker, daemon=True),
        threading.Thread(target=reader, daemon=True,
                         args=("sls.nginx", lambda: sls.get_logs(eng, "nginx-ingress", 0, now()))),
        threading.Thread(target=reader, daemon=True,
                         args=("sls.trace", lambda: sls.get_logs(eng, "trace", 0, now()))),
        threading.Thread(target=reader, daemon=True,
                         args=("cms.list", lambda: cms.list_metrics(eng))),
        threading.Thread(target=reader, daemon=True,
                         args=("world_status", lambda: eng.world_status())),
        threading.Thread(target=reader, daemon=True,
                         args=("realtime", lambda: eng.realtime_snapshot())),
    ]
    for t in ths:
        t.start()
    time.sleep(4)
    stop.set()
    for t in ths:
        t.join(timeout=3)

    assert not errors, f"并发下出现 {len(errors)} 次异常：{errors[:3]}"
    print("  ✓ 4 秒 1写+5读 并发无异常（修复前 8 秒 39 次 RuntimeError）")


def test_concurrent_actions_keep_state_consistent():
    """并发提交治理动作时，副本数不能因竞态而算错。"""
    eng = _fresh(5)
    svc = "order-service"
    eng.service_state[svc]["replicas"] = 2
    stop = threading.Event()

    def ticker():
        while not stop.is_set():
            eng.tick()

    th = threading.Thread(target=ticker, daemon=True)
    th.start()
    for i in range(10):
        actions.apply_action(eng, "scale_out", svc, {"replicas": 3 + i})
    stop.set()
    th.join(timeout=3)

    assert eng.service_state[svc]["replicas"] == 12, \
        f"最终副本数应为 12，实际 {eng.service_state[svc]['replicas']}"
    print("  ✓ 与 tick 并发的 10 次扩容，最终状态正确")


def main():
    groups = [
        ("世界演进", [
            test_world_self_check,
            test_tick_advances_and_generates_data,
            test_deterministic_with_same_seed,
            test_buffers_bounded,
        ]),
        ("故障注入与恢复", [
            test_inject_and_recover_roundtrip,
            test_recover_isolates_by_fault_id,
            test_unknown_scenario_rejected,
            test_scenario_metadata_complete,
        test_every_scenario_actually_moves_the_world,
        test_confusable_pairs_are_symmetric_and_distinct,
        ]),
        ("治理动作", [
            test_scale_out_changes_replicas,
            test_patch_resources_affects_oversale,
            test_unknown_action_rejected,
            test_action_recorded_in_recent,
        ]),
        ("渲染器契约", [
            test_cms_datapoints_is_json_string,
            test_sls_trace_and_log_formats,
            test_sls_unknown_logstore_and_paging,
        ]),
        ("并发安全", [
            test_no_race_between_tick_and_renderers,
            test_concurrent_actions_keep_state_consistent,
        ]),
    ]
    passed = failed = 0
    for title, tests in groups:
        print(f"\n=== {title} ===")
        for fn in tests:
            try:
                fn()
                passed += 1
            except Exception as e:                # noqa: BLE001
                failed += 1
                print(f"  ✗ {fn.__name__}: {e}")
                import traceback
                traceback.print_exc()
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
