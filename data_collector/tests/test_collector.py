"""data_collector 关键路径测试（此前该模块零测试）。

⚠️⚠️ 数据库隔离是本文件的第一要务 ⚠️⚠️

`collector/config.py` 读的是 **DB_URL**（不是 DATABASE_URL），且 `.env` 里
`DB_HOST` 指向**线上 RDS**。写这个测试时我一开始只设了 DATABASE_URL，
结果 `DELETE FROM` 打在了真实云数据库上，删掉了观测表数据。

所以本文件：
  1. 在 import collector 之前就把 DB_URL 强制指向临时 SQLite；
  2. 导入后立即做**安全断言**——只要连接串不是 sqlite 就直接退出，
     绝不允许"配置没生效"这件事以静默方式通过。

零依赖，直接运行：
    cd data_collector && ../backend/.venv/bin/python tests/test_collector.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

# ── 必须在导入 collector 之前设置：config 在模块导入时就会 build_db_url() ──
_TMP_DB = Path(tempfile.gettempdir()) / "collector_test.db"
_TMP_DB.unlink(missing_ok=True)
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"      # 保险：万一将来改读这个

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import config, db, realtime, retention, scheduled     # noqa: E402


def _assert_isolated():
    """安全闸门：连接串必须是本地 SQLite，否则立刻退出。

    这个测试会执行 DELETE / INSERT。一旦 DB_URL 没生效而指向了线上库，
    后果是真实数据被删。所以宁可整个测试跑不起来，也不能带着错的连接串继续。
    """
    url = config.DB_URL
    if not url.startswith("sqlite"):
        print("=" * 70)
        print("❌ 测试中止：数据库未隔离！")
        print(f"   当前 DB_URL = {url.split('@')[-1] if '@' in url else url}")
        print("   本测试会执行 DELETE，必须连本地 SQLite。")
        print("   请检查 collector/config.py 的 build_db_url() 是否仍优先读 DB_URL。")
        print("=" * 70)
        raise SystemExit(2)
    if str(_TMP_DB) not in url:
        print(f"❌ 测试中止：DB_URL 指向了非预期的 sqlite 文件：{url}")
        raise SystemExit(2)


_assert_isolated()


def _reset():
    for t in list(retention.OBSERVED_TABLES) + ["realtime_metrics", "k8s_resources"]:
        db.execute(f"DELETE FROM {t}")


# ══════════════════════════════════════════════════════
# 隔离防护自身
# ══════════════════════════════════════════════════════

def test_db_is_sqlite_temp():
    """★ 元测试：确认本测试确实跑在临时 SQLite 上，而非线上库。"""
    assert config.DB_URL.startswith("sqlite"), config.DB_URL
    assert str(_TMP_DB) in config.DB_URL
    assert "rds.aliyuncs.com" not in config.DB_URL
    print(f"  ✓ 隔离生效：{_TMP_DB.name}")


def test_db_url_prefers_env():
    """DB_URL 环境变量必须优先于 .env 的 DB_HOST —— 隔离机制依赖这一点。"""
    old = os.environ.get("DB_URL")
    try:
        os.environ["DB_URL"] = "sqlite:///probe.db"
        assert config.build_db_url() == "sqlite:///probe.db"
    finally:
        if old is not None:
            os.environ["DB_URL"] = old
    print("  ✓ DB_URL 优先级正确（隔离手段可靠）")


# ══════════════════════════════════════════════════════
# 字段映射（mock 响应 → 表结构）
# ══════════════════════════════════════════════════════

def test_ingress_mapping_types():
    """SLS 日志的值全是字符串，映射后必须转成正确类型入库。"""
    row = scheduled._map_ingress({
        "__time__": "1786200000", "method": "POST", "url": "/api/orders",
        "status": "500", "request_time": "1.234", "upstream_addr": "10.0.0.1",
        "upstream_status": "502", "client_ip": "1.2.3.4", "req_id": "r1",
        "__source__": "pod-1",
    })
    assert isinstance(row["ts"], int) and row["ts"] == 1786200000
    assert isinstance(row["status"], int) and row["status"] == 500
    assert isinstance(row["request_time"], float) and abs(row["request_time"] - 1.234) < 1e-9
    assert row["source_pod"] == "pod-1", "__source__ 应映射到 source_pod"
    print(f"  ✓ ingress 映射：ts={row['ts']} status={row['status']} rt={row['request_time']}")


def test_span_mapping_parses_json_strings():
    """格式坑：attribute/resource 是 JSON 字符串，入库要解开。"""
    row = scheduled._map_span({
        "__time__": 1786200000, "traceID": "t1", "spanID": "s1", "parentSpanID": "",
        "service": "order-service", "host": "pod-1", "name": "POST /api/orders",
        "kind": "server", "start": 1786200000000000, "duration": 1234000,
        "statusCode": "OK", "statusMessage": "",
        "attribute": '{"http.method":"POST"}', "resource": '{"pod":"p1"}',
    })
    attr = row["attr_json"]
    parsed = json.loads(attr) if isinstance(attr, str) else attr
    assert parsed["http.method"] == "POST", f"attribute 未解开：{attr!r}"
    assert row["duration_us"] == 1234000
    print("  ✓ span 映射：attribute 已解开、微秒字段保持整数")


def test_mapping_tolerates_bad_values():
    """脏数据（空串 / None）不能让整轮采集崩掉。"""
    row = scheduled._map_ingress({
        "__time__": "", "method": "GET", "url": "/x", "status": "abc",
        "request_time": None, "req_id": "",
    })
    assert row["ts"] == 0 and row["status"] == 0 and row["request_time"] == 0.0
    print("  ✓ 空串/非数字/None 均降级为默认值，不抛异常")


def test_slow_log_mapping_sets_governed_flag():
    """慢日志入库时 governed 必须初始化为 0，否则治理闭环判断会错。"""
    row = scheduled._map_slow({
        "__time__": "1786200000", "instance_id": "rds-1", "db_name": "orders",
        "sql_text": "SELECT 1", "query_time": "2.5", "lock_time": "0.01",
        "rows_examined": "1200000", "rows_sent": "10",
    })
    assert row["governed"] == 0
    assert row["rows_examined"] == 1200000
    print("  ✓ 慢日志映射：governed=0，rows_examined 为整数")


# ══════════════════════════════════════════════════════
# 水位线（增量去重的核心）
# ══════════════════════════════════════════════════════

def test_watermark_load_from_empty_db():
    _reset()
    wm = scheduled.Watermark()
    wm.load_from_db()
    # 注意 key 是【logstore 名】而不是表名（scheduled.LOGSTORE_TABLES 的键）
    for key in list(scheduled.LOGSTORE_TABLES) + ["metrics"]:
        assert wm.get(key) == 0, f"空表水位应为 0，实际 {wm.get(key)}"
    print(f"  ✓ 空库水位线全为 0（{len(scheduled.LOGSTORE_TABLES) + 1} 个 logstore）")


def test_watermark_load_reflects_existing_data():
    _reset()
    db.execute("""INSERT INTO ingress_logs (ts, method, url, status, request_time)
                  VALUES (:ts,'GET','/x',200,0.1)""", {"ts": 1786200000})
    wm = scheduled.Watermark()
    wm.load_from_db()
    # ingress_logs 表对应的 logstore 名是 nginx-ingress
    assert wm.get("nginx-ingress") == 1786200000, wm.get("nginx-ingress")
    print(f"  ✓ 水位线从已有数据恢复：nginx-ingress={wm.get('nginx-ingress')}")


def test_watermark_is_monotonic():
    """★ 水位线只能前进不能后退：否则乱序响应会导致重复采集同一批数据。"""
    wm = scheduled.Watermark()
    wm.set("app-log", 456)
    assert wm.get("app-log") == 456
    wm.set("app-log", 123)                      # 更小的值应被忽略
    assert wm.get("app-log") == 456, "水位线倒退了，会造成重复采集"
    wm.set("app-log", 789)
    assert wm.get("app-log") == 789
    assert wm.get("never_set_key") == 0, "未设置的键应返回 0 而不是 KeyError"
    print("  ✓ 水位线单调递增（更小的值被忽略），未知键返回 0")


# ══════════════════════════════════════════════════════
# 保留期清理
# ══════════════════════════════════════════════════════

def test_retention_respects_per_table_unit():
    """★ 观测表 ts 单位不统一（秒/毫秒），清理必须按各表单位换算。

    统一当秒处理：metrics（毫秒）的 cutoff 小上千倍 → 一条都删不掉；
    统一当毫秒处理：日志表会被整表清空。
    """
    _reset()
    now_s = int(time.time())
    old_s = now_s - int(config.RETENTION_HOURS * 3600) - 3600     # 超期 1 小时

    for ts, tag in ((old_s, "/old"), (now_s, "/new")):
        db.execute("""INSERT INTO ingress_logs (ts, method, url, status, request_time)
                      VALUES (:ts,'GET',:u,200,0.1)""", {"ts": ts, "u": tag})
    for ts, tag in ((old_s * 1000, "old"), (now_s * 1000, "new")):
        db.execute("""INSERT INTO metrics (namespace, metric_name, dims_json, ts, avg, max, min)
                      VALUES ('ns',:m,'{}',:ts,1,1,1)""", {"m": tag, "ts": ts})

    deleted = retention.run_once()
    left_logs = [r["url"] for r in db.fetch_all("SELECT url FROM ingress_logs")] \
        if hasattr(db, "fetch_all") else None
    if left_logs is None:                       # collector.db 只有 scalar/execute
        left_logs = _rows("SELECT url FROM ingress_logs", "url")
    left_metrics = _rows("SELECT metric_name FROM metrics", "metric_name")
    assert left_logs == ["/new"], f"秒表清理错误，剩下 {left_logs}"
    assert left_metrics == ["new"], f"毫秒表清理错误，剩下 {left_metrics}（单位换算错）"
    print(f"  ✓ 秒表与毫秒表都只删过期：{deleted}")


def _rows(sql: str, col: str) -> list:
    """collector.db 没有 fetch_all，用 engine 直查。"""
    from sqlalchemy import text
    with db.get_engine().connect() as conn:
        return [r[0] for r in conn.execute(text(sql))]


def test_retention_covers_all_growing_tables():
    """★ 新增观测表必须登记保留期，否则它会无限增长。

    实测发现 k8s_resources 未登记 —— 它是 replace_table 全量覆盖的快照表
    （行数恒定），因此不需要按时间清理；本用例把这个判断显式化，
    以后新增的**追加型**表若漏登记就会被抓住。
    """
    declared = set(retention.OBSERVED_TABLES) | {"realtime_metrics"}
    # 全量覆盖型（每轮 replace，不会增长），无需时间清理
    snapshot_tables = {"k8s_resources"}
    all_tables = {t.name for t in db.metadata.tables.values()}
    unmanaged = all_tables - declared - snapshot_tables
    assert not unmanaged, (
        f"这些表既未登记保留期、也不是快照表，会无限增长：{sorted(unmanaged)}")
    print(f"  ✓ {len(declared)} 张追加表已登记保留期，"
          f"{len(snapshot_tables)} 张快照表全量覆盖")


# ══════════════════════════════════════════════════════
# 实时通道
# ══════════════════════════════════════════════════════

def test_realtime_flatten():
    """/realtime/metrics 的嵌套响应要按 METRIC_PATHS 展平成逐指标行。"""
    resp = {"ts": 1786200000000, "items": [{
        "instance": "pod-1", "service": "order-service", "kind": "pod", "status": "Running",
        "water_level": {"cpu_pct": 55.5, "mem_pct": 60.0, "conn_pct": 0.0},
        "capacity": {"capacity_pct": 70.0, "replicas": 3},
    }]}
    rows = realtime.flatten(resp)
    assert rows, "展平结果为空"
    metrics = {r["metric"] for r in rows}
    assert {"cpu_pct", "mem_pct", "capacity_pct", "replicas"} <= metrics, metrics
    assert "conn_pct" in metrics, "值为 0 的指标也应保留（便于统一查询）"
    for r in rows:
        assert r["instance"] == "pod-1" and r["service"] == "order-service"
        assert isinstance(r["value"], float), f"value 应为 float：{r}"
        assert r["ts"] == 1786200000000
    print(f"  ✓ 展平出 {len(rows)} 行；指标 {sorted(metrics)}")


def test_realtime_flatten_skips_missing_metrics():
    """缺失的组（如 Pod 无 max_conn）应跳过，而不是写 None 进库。"""
    resp = {"ts": 1, "items": [{
        "instance": "i", "service": "s", "kind": "pod", "status": "Running",
        "water_level": {"cpu_pct": 10.0},
    }]}
    rows = realtime.flatten(resp)
    assert [r["metric"] for r in rows] == ["cpu_pct"], [r["metric"] for r in rows]
    assert all(r["value"] is not None for r in rows)
    print("  ✓ 缺失指标被跳过，不会写 None")


def test_realtime_flatten_empty():
    assert realtime.flatten({"ts": 1}) == []
    assert realtime.flatten({"ts": 1, "items": []}) == []
    print("  ✓ 空响应返回空列表")


# ══════════════════════════════════════════════════════
# 失败分级（本次新增）
# ══════════════════════════════════════════════════════

def test_failure_escalation_thresholds():
    """连续失败达阈值后升级为 ERROR，并给出断流时长与排查指引。"""
    from collector import main as collector_main

    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, record.getMessage()))

    h = _Cap()
    collector_main.logger.addHandler(h)
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            collector_main._on_failure("定时", 1, 60)
        assert not any("连续失败" in m for _, m in records), \
            "首次失败不应报连续失败告警"

        collector_main._on_failure("定时", collector_main._ALERT_AFTER, 60)
        alerts = [m for lvl, m in records if lvl >= logging.ERROR and "连续失败" in m]
        assert alerts, f"未升级为连续失败告警：{records}"
        assert "秒无新数据" in alerts[-1], alerts[-1]
        assert "mock_server" in alerts[-1], "缺少排查指引"
    finally:
        collector_main.logger.removeHandler(h)
    print(f"  ✓ 连续失败 {collector_main._ALERT_AFTER} 次后升级告警，含断流时长与指引")


def test_client_retry_bounded_and_returns_none():
    """★ client.get 全部重试失败后返回 None（而不是抛异常）。

    调用方靠 `if not resp: return 0` 跳过本轮。若改成抛异常，
    scheduled/realtime 的 run_once 就会中断，日志变成栈刷屏。
    """
    from collector.client import MockClient

    calls = {"n": 0}

    class _FakeHttpx:
        async def get(self, *a, **kw):
            calls["n"] += 1
            raise RuntimeError("network down")

        async def aclose(self):
            pass

    async def run():
        c = MockClient()
        c._client = _FakeHttpx()                # httpx.AsyncClient 的位置
        # 把退避睡眠掐掉，否则 1+2s 让测试变慢
        orig_sleep = asyncio.sleep

        async def fast_sleep(_s):
            await orig_sleep(0)

        import collector.client as cli
        cli.asyncio.sleep = fast_sleep
        try:
            return await c.get("/x", retries=3)
        finally:
            cli.asyncio.sleep = orig_sleep

    result = asyncio.run(run())
    assert result is None, f"全部失败应返回 None，实际 {result!r}"
    assert calls["n"] == 3, f"应恰好尝试 retries=3 次，实际 {calls['n']}"
    print(f"  ✓ 尝试 {calls['n']} 次后返回 None（调用方可跳过本轮而不崩）")


def main():
    db.init_db()
    groups = [
        ("★ 数据库隔离防护", [
            test_db_is_sqlite_temp,
            test_db_url_prefers_env,
        ]),
        ("字段映射", [
            test_ingress_mapping_types,
            test_span_mapping_parses_json_strings,
            test_mapping_tolerates_bad_values,
            test_slow_log_mapping_sets_governed_flag,
        ]),
        ("水位线", [
            test_watermark_load_from_empty_db,
            test_watermark_load_reflects_existing_data,
            test_watermark_is_monotonic,
        ]),
        ("保留期清理", [
            test_retention_respects_per_table_unit,
            test_retention_covers_all_growing_tables,
        ]),
        ("实时通道", [
            test_realtime_flatten,
            test_realtime_flatten_skips_missing_metrics,
            test_realtime_flatten_empty,
        ]),
        ("失败分级与重试", [
            test_failure_escalation_thresholds,
            test_client_retry_bounded_and_returns_none,
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
