"""U2 有状态 tick 引擎与实时数据发生器

每 TICK_INTERVAL_S 推进世界一拍，演算顺序：
    基线 + 日内噪声 + 故障效应（faults 注入的 effects）+ 动作效应（actions 的恢复曲线）

产出三类缓冲（供 renderers 消费）：
- metric_buffer : 分钟级 CMS 数据点，环形保留 60 分钟
- log_buffer    : ingress / app / slow / k8s-event 日志事件，保留 30 分钟
- trace_buffer  : span 列表（按 TRACE_SAMPLE_RATE 采样），保留 30 分钟

设计约束：
- 同 seed 同时间轴可复现（随机源统一走 self.rng）
- 实时查询（/realtime/metrics）直接读当前拍状态，毫秒返回，不做任何生成
- effects 由 faults/actions 模块写入 self.effects，引擎只负责"把效应叠加到演算里"，
  故障传播算法本身在 faults.py（职责分离）
"""
from __future__ import annotations

import math
import random
import threading
import time
import uuid
from collections import deque
from typing import Dict, List

from . import config, world_def as W


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


class WorldEngine:
    """世界引擎单例。effects 是故障/动作对基线的乘加修正，由 faults/actions 模块维护。"""

    def __init__(self, seed: int = None):
        # ★ 并发保护：tick 在线程池里【写】世界状态，而 12 个同步路由同时在别的
        #   线程【读】同一批 buffer / state。实测无锁时 8 秒内会出现 39 次
        #   RuntimeError('deque mutated during iteration')。
        #   用 RLock 而非 Lock：tick 内部会调用同样持锁的方法（如 world_status
        #   调 current_oversale_pct），可重入才不会自己把自己锁死。
        #   跨模块的"读-改-写"复合操作（faults / actions）请用 `with engine.lock:`。
        self.lock = threading.RLock()
        self.rng = random.Random(seed if seed is not None else config.WORLD_SEED)
        self.tick_no = 0
        self.world_version = 0
        self.tick_ts_ms = int(time.time() * 1000)
        self.started_at_ms = self.tick_ts_ms

        # 实例运行时状态：instance -> {cpu_pct, mem_pct, conn_pct, ...}
        self.instances: Dict[str, dict] = {}
        # 接口运行时状态："METHOD path" -> {qps, p99_ms, error_rate}
        self.apis: Dict[str, dict] = {}
        # 服务级可变配置（治理动作会改它：replicas / resources / probes / pdb / zone_affinity）
        self.service_state: Dict[str, dict] = {}
        # 存储级可变配置（max_connections / slow_query_tables）
        self.datastore_state: Dict[str, dict] = {}

        # 效应表：由 faults.py / actions.py 维护
        #   effects[entity] = {"metric_key": {"add": x, "mul": y, "until_tick": n|None, "source": "..."}}
        self.effects: Dict[str, Dict[str, dict]] = {}
        self.active_faults: List[dict] = []
        self.recent_actions: deque = deque(maxlen=20)
        self.audit: deque = deque(maxlen=100)

        # 数据缓冲
        max_metric_points = config.CMS_WINDOW_MINUTES + 1
        self.metric_buffer: Dict[str, deque] = {}          # "ns|metric|dimkey" -> deque[(ts_ms, value)]
        self._metric_meta: Dict[str, dict] = {}            # 同键 -> 维度信息
        self._metric_maxlen = max_metric_points
        self.log_buffer: Dict[str, deque] = {
            "nginx-ingress": deque(maxlen=20000),
            "app-log": deque(maxlen=5000),
            "rds_slow_log": deque(maxlen=2000),
            "k8s-events": deque(maxlen=1000),
        }
        self.trace_buffer: deque = deque(maxlen=30000)
        self._last_minute_bucket = -1

        self._init_state()

    # ------------------------------------------------------------------ 初始化

    def _init_state(self):
        for inst in W.all_instances():
            name = inst["instance"]
            if inst["kind"] == "pod":
                cfg = W.SERVICES[inst["service"]]
                self.instances[name] = {
                    **inst,
                    "cpu_pct": cfg["cpu_base_pct"], "mem_pct": cfg["mem_base_pct"],
                    "conn_pct": 0.0,
                    "bandwidth_in_mbps": cfg["bandwidth_base_mbps"] * 0.6,
                    "bandwidth_out_mbps": cfg["bandwidth_base_mbps"] * 0.4,
                    "restart_count": 0, "status": "normal", "healthy": True,
                }
            else:
                cfg = W.DATASTORES[inst["service"]]
                self.instances[name] = {
                    **inst,
                    "cpu_pct": cfg["cpu_base_pct"], "mem_pct": cfg["mem_base_pct"],
                    "conn_pct": cfg["conn_base_pct"],
                    "disk_pct": cfg.get("disk_base_pct", 0.0),
                    "iops_pct": cfg.get("iops_base_pct", 0.0),
                    "bandwidth_in_mbps": cfg["bandwidth_base_mbps"] * 0.5,
                    "bandwidth_out_mbps": cfg["bandwidth_base_mbps"] * 0.5,
                    "restart_count": 0, "status": "normal", "healthy": True,
                }

        for svc, cfg in W.SERVICES.items():
            self.service_state[svc] = {
                "replicas": cfg["replicas"],
                "requests": dict(cfg["requests"]), "limits": dict(cfg["limits"]),
                "probes": cfg["probes"], "pdb": cfg["pdb"],
                "zone_affinity": cfg.get("zone_affinity"),
                "healthy_replicas": cfg["replicas"],
            }
        for name, cfg in W.DATASTORES.items():
            self.datastore_state[name] = {
                "max_connections": cfg["max_connections"],
                # Redis 无慢查询概念，用 get 兜底
                "slow_query_tables": list(cfg.get("slow_query_tables", [])),
                "indexed_tables": [],
            }
        for api in W.APIS:
            self.apis[f"{api['method']} {api['path']}"] = {
                "qps": config.ENTRY_RPS * api["weight"],
                "p99_ms": float(api["p99_ms"]), "error_rate": api["error_rate"],
            }

    # ------------------------------------------------------------------ 效应

    def effect_of(self, entity: str, key: str) -> tuple:
        """返回 (add, mul)：该实体某指标当前受到的故障/动作叠加效应。"""
        e = self.effects.get(entity, {}).get(key)
        if not e:
            return 0.0, 1.0
        if e.get("until_tick") is not None and self.tick_no > e["until_tick"]:
            return 0.0, 1.0
        decay = e.get("decay_from_tick")
        if decay is not None:
            # 动作生效后按半衰期指数衰减回基线
            elapsed = self.tick_no - decay
            factor = 0.5 ** (elapsed / config.RECOVERY_HALFLIFE_TICKS)
            if factor < 0.02:
                return 0.0, 1.0
            return e.get("add", 0.0) * factor, 1.0 + (e.get("mul", 1.0) - 1.0) * factor
        return e.get("add", 0.0), e.get("mul", 1.0)

    def set_effect(self, entity: str, key: str, add: float = 0.0, mul: float = 1.0,
                   source: str = "", until_tick: int = None):
        self.effects.setdefault(entity, {})[key] = {
            "add": add, "mul": mul, "source": source, "until_tick": until_tick,
            "decay_from_tick": None,
        }

    def start_decay(self, entity: str, key: str = None):
        """让效应开始衰减（治理动作生效 / 故障恢复）。key=None 表示该实体全部效应。"""
        for k, e in self.effects.get(entity, {}).items():
            if key is None or k == key:
                e["decay_from_tick"] = self.tick_no

    def clear_effects(self, source_prefix: str):
        """清除某来源的全部效应（如某个 fault_id）。"""
        for entity in list(self.effects):
            for k in list(self.effects[entity]):
                if self.effects[entity][k].get("source", "").startswith(source_prefix):
                    del self.effects[entity][k]
            if not self.effects[entity]:
                del self.effects[entity]

    def record_audit(self, kind: str, detail: str):
        self.world_version += 1
        self.audit.append({"world_version": self.world_version, "tick": self.tick_no,
                           "ts_ms": self.tick_ts_ms, "kind": kind, "detail": detail})

    # ------------------------------------------------------------------ tick

    def tick(self):
        """推进一拍：演算状态 → 生成流量与日志 → 按分钟落 CMS 点。"""
        with self.lock:
            return self._tick_locked()

    def _tick_locked(self):
        self.tick_no += 1
        self.tick_ts_ms = int(time.time() * 1000)

        self._tick_apis()
        self._tick_instances()

        tick_stats = self._gen_traffic()
        self._gen_slow_logs()
        self._prune_buffers()

        minute_bucket = self.tick_ts_ms // 60000
        if minute_bucket != self._last_minute_bucket:
            self._last_minute_bucket = minute_bucket
            self._emit_cms_points(minute_bucket * 60000)
        return tick_stats

    def _daily_wave(self) -> float:
        """日内波动系数：以 10 分钟为周期做温和起伏（演示可见但不剧烈）。"""
        t = (self.tick_ts_ms - self.started_at_ms) / 1000.0
        return 1.0 + 0.12 * math.sin(2 * math.pi * t / 600.0)

    def _tick_apis(self):
        wave = self._daily_wave()
        for api in W.APIS:
            key = f"{api['method']} {api['path']}"
            st = self.apis[key]
            backend = api["backend"]

            qps_add, qps_mul = self.effect_of(backend, "qps")
            st["qps"] = max(0.05, config.ENTRY_RPS * api["weight"] * wave * qps_mul + qps_add)

            # 时延：基线 + 后端服务效应 + 该接口涉及的数据库效应
            lat_add, lat_mul = self.effect_of(backend, "latency_ms")
            base = api["p99_ms"] * (0.94 + 0.12 * self.rng.random())
            db_extra = 0.0
            for ds, table, _op in api["db_calls"]:
                ds_state = self.datastore_state[ds]
                if table in ds_state["slow_query_tables"] and table not in ds_state["indexed_tables"]:
                    db_extra += 900.0 + 200.0 * self.rng.random()
                d_add, d_mul = self.effect_of(ds, "latency_ms")
                db_extra += d_add * 0.9 + api["p99_ms"] * 0.1 * (d_mul - 1.0)
            st["p99_ms"] = max(5.0, base * lat_mul + lat_add + db_extra)

            err_add, err_mul = self.effect_of(backend, "error_rate")
            db_err = 0.0
            for ds, _t, _op in api["db_calls"]:
                e_add, _ = self.effect_of(ds, "error_rate")
                db_err += e_add
            # 后端副本全挂 → 该接口全错
            if self.service_state.get(backend, {}).get("healthy_replicas", 1) <= 0:
                st["error_rate"] = 1.0
            else:
                st["error_rate"] = _clamp(api["error_rate"] * err_mul + err_add + db_err, 0.0, 1.0)

    def _tick_instances(self):
        wave = self._daily_wave()
        # 各服务承载的 RPS（供水位演算）
        svc_rps: Dict[str, float] = {}
        for api in W.APIS:
            key = f"{api['method']} {api['path']}"
            svc_rps[api["backend"]] = svc_rps.get(api["backend"], 0.0) + self.apis[key]["qps"]
        # 上游服务的调用会传导到下游（含 DB）
        for src, dst in W.EDGES:
            svc_rps[dst] = svc_rps.get(dst, 0.0) + svc_rps.get(src, 0.0) * 0.55

        for name, st in self.instances.items():
            svc = st["service"]
            noise = lambda amp: (self.rng.random() - 0.5) * 2 * amp  # noqa: E731

            if st["kind"] == "pod":
                cfg = W.SERVICES[svc]
                healthy = self.service_state[svc]["healthy_replicas"]
                replicas = max(1, self.service_state[svc]["replicas"])
                # 副本减少 → 存活实例分摊更多负载
                load_factor = replicas / max(1, healthy) if healthy > 0 else 1.0
                cpu_add, cpu_mul = self.effect_of(name, "cpu_pct")
                s_cpu_add, s_cpu_mul = self.effect_of(svc, "cpu_pct")
                st["cpu_pct"] = _clamp(
                    cfg["cpu_base_pct"] * wave * load_factor * cpu_mul * s_cpu_mul
                    + cpu_add + s_cpu_add + noise(2.0))
                mem_add, mem_mul = self.effect_of(name, "mem_pct")
                s_mem_add, s_mem_mul = self.effect_of(svc, "mem_pct")
                st["mem_pct"] = _clamp(
                    cfg["mem_base_pct"] * mem_mul * s_mem_mul + mem_add + s_mem_add + noise(1.5))
                cap_mbps = cfg["bandwidth_capacity_mbps"]
                bw_add, bw_mul = self.effect_of(name, "bandwidth_mbps")
                s_bw_add, s_bw_mul = self.effect_of(svc, "bandwidth_mbps")
                total_bw = (cfg["bandwidth_base_mbps"] * wave * bw_mul * s_bw_mul
                            + bw_add + s_bw_add + noise(6.0))
                total_bw = max(1.0, min(cap_mbps, total_bw))
                st["bandwidth_in_mbps"] = round(total_bw * 0.58, 2)
                st["bandwidth_out_mbps"] = round(total_bw * 0.42, 2)
                st["bandwidth_capacity_mbps"] = cap_mbps
                st["conn_pct"] = 0.0
            else:
                cfg = W.DATASTORES[svc]
                rps = svc_rps.get(svc, 1.0)
                conn_add, conn_mul = self.effect_of(name, "conn_pct")
                base_conn = cfg["conn_base_pct"] * (0.85 + 0.3 * rps / max(1.0, config.ENTRY_RPS))
                st["conn_pct"] = _clamp(base_conn * conn_mul + conn_add + noise(1.5))
                cpu_add, cpu_mul = self.effect_of(name, "cpu_pct")
                st["cpu_pct"] = _clamp(cfg["cpu_base_pct"] * wave * cpu_mul + cpu_add + noise(2.0))
                mem_add, mem_mul = self.effect_of(name, "mem_pct")
                st["mem_pct"] = _clamp(cfg["mem_base_pct"] * mem_mul + mem_add + noise(1.0))
                st["disk_pct"] = _clamp(cfg.get("disk_base_pct", 0.0) + noise(0.5))
                st["iops_pct"] = _clamp(cfg.get("iops_base_pct", 0.0) * wave + noise(2.0))
                cap_mbps = cfg["bandwidth_capacity_mbps"]
                bw_add, bw_mul = self.effect_of(name, "bandwidth_mbps")
                total_bw = max(1.0, min(cap_mbps, cfg["bandwidth_base_mbps"] * wave * bw_mul
                                        + bw_add + noise(8.0)))
                st["bandwidth_in_mbps"] = round(total_bw * 0.5, 2)
                st["bandwidth_out_mbps"] = round(total_bw * 0.5, 2)
                st["bandwidth_capacity_mbps"] = cap_mbps

            # 状态判定（供前端着色）
            worst = max(st["cpu_pct"], st["mem_pct"], st["conn_pct"])
            if not st.get("healthy", True):
                st["status"] = "faulty"
            elif worst >= 85.0:
                st["status"] = "faulty"
            elif worst >= 70.0:
                st["status"] = "degraded"
            else:
                st["status"] = "normal"

    # ------------------------------------------------------------------ 流量与日志

    def _gen_traffic(self) -> dict:
        """按各接口 qps 生成本拍请求：全部落 ingress 日志，按采样率生成 trace span 树。"""
        total_req = 0
        total_err = 0
        sampled = 0
        for api in W.APIS:
            key = f"{api['method']} {api['path']}"
            st = self.apis[key]
            n = self._poisson(st["qps"] * config.TICK_INTERVAL_S)
            for _ in range(n):
                total_req += 1
                is_err = self.rng.random() < st["error_rate"]
                if is_err:
                    total_err += 1
                # 单请求耗时：以 p99 为上界做偏态分布
                lat_ms = st["p99_ms"] * (0.25 + 0.75 * self.rng.random() ** 2.2)
                if self.rng.random() < 0.01:
                    lat_ms = st["p99_ms"] * (0.95 + 0.1 * self.rng.random())
                trace_id = uuid.uuid4().hex
                do_sample = self.rng.random() < config.TRACE_SAMPLE_RATE
                self._emit_ingress_log(api, lat_ms, is_err, trace_id)
                if do_sample:
                    sampled += 1
                    self._emit_trace(api, lat_ms, is_err, trace_id)
                if is_err:
                    self._emit_error_app_log(api)
        return {"tick": self.tick_no, "requests": total_req, "errors": total_err,
                "error_rate": round(total_err / total_req, 4) if total_req else 0.0,
                "sampled_traces": sampled}

    def _poisson(self, lam: float) -> int:
        """小 lambda 的泊松采样（Knuth），保证请求数有自然抖动。"""
        if lam <= 0:
            return 0
        limit = math.exp(-lam)
        k, p = 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1
            if k > 500:
                return k

    def _pick_pod(self, service: str) -> dict:
        """在健康实例中挑一个（副本挂掉后不会被选中）。"""
        pods = [i for i in self.instances.values()
                if i["service"] == service and i["kind"] == "pod" and i.get("healthy", True)]
        if not pods:
            pods = [i for i in self.instances.values()
                    if i["service"] == service and i["kind"] == "pod"]
        return self.rng.choice(pods)

    def _emit_ingress_log(self, api: dict, lat_ms: float, is_err: bool, trace_id: str):
        ing = self._pick_pod("nginx-ingress")
        fe = self._pick_pod("web-frontend")
        status = self.rng.choice([500, 502, 504]) if is_err else 200
        self.log_buffer["nginx-ingress"].append({
            "__time__": str(self.tick_ts_ms // 1000),
            "__topic__": "nginx-ingress",
            "__source__": ing["instance"],
            "client_ip": f"{self.rng.randint(36, 220)}.{self.rng.randint(0, 255)}."
                         f"{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}",
            "remote_user": "-",
            "time_local": time.strftime("%d/%b/%Y:%H:%M:%S +0800",
                                        time.localtime(self.tick_ts_ms / 1000)),
            "method": api["method"], "url": api["path"], "version": "HTTP/1.1",
            "status": str(status),
            "body_bytes_sent": str(self.rng.randint(200, 4000)),
            "http_referer": "https://shop.example.com/",
            "http_user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "request_length": str(self.rng.randint(300, 1200)),
            "request_time": f"{lat_ms / 1000:.3f}",
            "proxy_upstream_name": "default-web-frontend-80",
            "upstream_addr": f"{fe['ip']}:80",
            "upstream_response_time": f"{max(0.001, lat_ms / 1000 - 0.003):.3f}",
            "upstream_status": str(status),
            "req_id": trace_id,          # KTD-6：req_id == traceID
            "host": "shop.example.com",
        })

    def _emit_error_app_log(self, api: dict):
        pod = self._pick_pod(api["backend"])
        # 错误文案按当前故障类型选取，保证日志与指标同源
        text = W.ERROR_TEXTS["upstream_5xx"]
        for ds, table, _op in api["db_calls"]:
            ds_state = self.datastore_state[ds]
            if table in ds_state["slow_query_tables"] and table not in ds_state["indexed_tables"]:
                text = W.ERROR_TEXTS["db_timeout"]
                break
            if self.instances[ds]["conn_pct"] >= 85.0:
                text = (f"{W.ERROR_TEXTS['db_conn']} "
                        f"(host={W.DATASTORES[ds]['endpoint']}:{W.DATASTORES[ds]['port']}, "
                        f"db={W.DATASTORES[ds]['db_name']})")
                break
        if self.service_state.get(api["backend"], {}).get("healthy_replicas", 1) <= 0:
            text = W.ERROR_TEXTS["pod_unavailable"]
        self.log_buffer["app-log"].append({
            "__time__": str(self.tick_ts_ms // 1000), "__topic__": "app-log",
            "__source__": pod["instance"], "level": "ERROR", "pod_ip": pod["ip"],
            "message": f"failed to handle {api['method']} {api['path']}: {text}",
        })

    def _emit_trace(self, api: dict, lat_ms: float, is_err: bool, trace_id: str):
        """生成 span 树：ingress(server) → client → frontend → ... → backend → db/cache。"""
        start_us = self.tick_ts_ms * 1000
        total_us = int(lat_ms * 1000)
        span_name = f"{api['method']} {api['path']}"
        status = "ERROR" if is_err else "OK"
        msg = ""
        if is_err:
            msg = W.ERROR_TEXTS["upstream_5xx"]

        chain = ["nginx-ingress"]
        if api["backend"] != "web-frontend":
            chain += ["web-frontend", "api-gateway"]
        else:
            chain += ["web-frontend"]
        if api["backend"] not in chain:
            chain.append(api["backend"])

        spans = []
        parent_id = ""
        remaining = total_us
        for depth, svc in enumerate(chain):
            pod = self._pick_pod(svc)
            span_id = uuid.uuid4().hex[:16]
            dur = int(remaining * (0.97 if depth < len(chain) - 1 else 1.0))
            spans.append(self._span(trace_id, span_id, parent_id, svc, pod, "server",
                                    span_name, start_us, dur, status, msg, api))
            if depth < len(chain) - 1:
                client_id = uuid.uuid4().hex[:16]
                spans.append(self._span(trace_id, client_id, span_id, svc, pod, "client",
                                        f"call {chain[depth + 1]}", start_us,
                                        int(dur * 0.96), status, msg, api))
                parent_id = client_id
            else:
                parent_id = span_id
            remaining = int(dur * 0.94)

        # DB / cache 子 span（挂在 backend 的 server span 下）
        backend_pod = self._pick_pod(api["backend"])
        for ds, table, op in api["db_calls"]:
            ds_state = self.datastore_state[ds]
            is_slow = table in ds_state["slow_query_tables"] and table not in ds_state["indexed_tables"]
            db_dur = int((900_000 + self.rng.randint(0, 250_000)) if is_slow
                         else W.DATASTORES[ds]["base_latency_ms"] * 1000 * (0.8 + 0.6 * self.rng.random()))
            stmt = (self.rng.choice(W.SLOW_SQLS) if is_slow
                    else f"{op} FROM {table} WHERE id = {self.rng.randint(1000, 99999)}")
            db_status = "ERROR" if (is_err or is_slow and self.rng.random() < 0.3) else "OK"
            db_msg = W.ERROR_TEXTS["db_timeout"] if db_status == "ERROR" and is_slow else msg
            spans.append(self._span(
                trace_id, uuid.uuid4().hex[:16], parent_id, api["backend"], backend_pod,
                "client", f"{op} {table}", start_us, db_dur, db_status, db_msg, api,
                peer=ds, db_statement=stmt))
        for cache in api["cache_calls"]:
            spans.append(self._span(
                trace_id, uuid.uuid4().hex[:16], parent_id, api["backend"], backend_pod,
                "client", f"GET kvstore", start_us,
                int(W.DATASTORES[cache]["base_latency_ms"] * 1000 * (0.8 + 0.8 * self.rng.random())),
                "OK", "", api, peer=cache,
                db_statement=f"GET cache:{self.rng.randint(1000, 99999)}"))

        self.trace_buffer.extend(spans)

    def _span(self, trace_id, span_id, parent_id, service, pod, kind, name,
              start_us, dur_us, status, msg, api, peer=None, db_statement=None) -> dict:
        attribute = {"http.method": api["method"],
                     "http.url": f"http://shop.example.com{api['path']}",
                     "http.status_code": 500 if status == "ERROR" else 200}
        if peer:
            ds = W.DATASTORES[peer]
            attribute.update({
                "db.system": "mysql" if ds["kind"] == "rds" else "redis",
                "db.name": ds["db_name"], "db.statement": db_statement,
                "peer.service": peer, "net.peer.name": ds["endpoint"], "net.peer.port": ds["port"],
            })
        elif kind == "client":
            target = name.replace("call ", "")
            attribute.update({
                "peer.service": target,
                "net.peer.name": f"{target}.default.svc.cluster.local", "net.peer.port": 8080,
            })
        return {
            "traceID": trace_id, "spanID": span_id, "parentSpanID": parent_id,
            "service": service, "host": pod["instance"], "name": name, "kind": kind,
            "start": start_us, "end": start_us + dur_us, "duration": dur_us,
            "statusCode": status, "statusMessage": msg,
            "attribute": attribute,
            "resource": {"service.name": service, "host.name": pod["instance"],
                         "k8s.pod.ip": pod["ip"], "k8s.namespace": pod.get("namespace") or "default",
                         "k8s.cluster.name": W.CLUSTER},
            "__time__": self.tick_ts_ms // 1000,
        }

    def _gen_slow_logs(self):
        """慢查询表非空 ⇒ 每拍产出若干慢日志（rows_examined 百万级、rows_sent 个位数）。"""
        for ds_name, ds_state in self.datastore_state.items():
            pending = [t for t in ds_state["slow_query_tables"] if t not in ds_state["indexed_tables"]]
            if not pending:
                continue
            for _ in range(self.rng.randint(2, 5)):
                sql = self.rng.choice(W.SLOW_SQLS)
                src_pod = self._pick_pod("order-service")
                qt = 1.2 + self.rng.random() * 1.8
                self.log_buffer["rds_slow_log"].append({
                    "__time__": str(self.tick_ts_ms // 1000), "__topic__": "rds_slow_log",
                    "__source__": ds_name, "instance_id": ds_name,
                    "db_name": W.DATASTORES[ds_name]["db_name"], "sql_text": sql,
                    "query_time": f"{qt:.3f}", "lock_time": f"{self.rng.random() * 0.01:.5f}",
                    "rows_examined": str(self.rng.randint(1_050_000, 1_400_000)),
                    "rows_sent": str(self.rng.randint(0, 8)),
                    "user_host": f"app_user[app_user] @ [{src_pod['ip']}]",
                    "start_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                                time.localtime(self.tick_ts_ms / 1000)),
                })
                # 慢 SQL 文本三处同源：应用日志 WARN
                self.log_buffer["app-log"].append({
                    "__time__": str(self.tick_ts_ms // 1000), "__topic__": "app-log",
                    "__source__": src_pod["instance"], "level": "WARN", "pod_ip": src_pod["ip"],
                    "message": f"slow query detected ({qt:.1f}s): {sql}",
                })

    def emit_k8s_event(self, event_type: str, reason: str, message: str,
                       obj_kind: str, obj_name: str, namespace: str = "default"):
        ts = str(self.tick_ts_ms // 1000)
        self.log_buffer["k8s-events"].append({
            "__time__": ts, "__topic__": "k8s-events", "__source__": f"{namespace}/{obj_name}",
            "event_type": event_type, "reason": reason, "message": message,
            "pod_name": obj_name if obj_kind == "Pod" else "",
            "namespace": namespace, "cluster": W.CLUSTER,
            "involved_object_kind": obj_kind, "involved_object_name": obj_name,
            "count": "1", "first_timestamp": ts, "last_timestamp": ts,
            "reporting_controller": "kubelet" if obj_kind == "Pod" else "deployment-controller",
        })

    # ------------------------------------------------------------------ CMS

    def _push_metric(self, ns: str, metric: str, dims: dict, value: float, ts_ms: int):
        dim_key = "|".join(f"{k}={v}" for k, v in sorted(dims.items()))
        key = f"{ns}|{metric}|{dim_key}"
        if key not in self.metric_buffer:
            self.metric_buffer[key] = deque(maxlen=self._metric_maxlen)
            self._metric_meta[key] = {"namespace": ns, "metric": metric, "dims": dims}
        self.metric_buffer[key].append((ts_ms, round(value, 2)))

    def _emit_cms_points(self, ts_ms: int):
        """按分钟落 CMS 数据点（与现有 16 组指标结构对齐，另加 2 个 RDS 实例）。"""
        for name, st in self.instances.items():
            if st["kind"] == "pod":
                dims = {"userId": W.USER_ID, "cluster": W.CLUSTER,
                        "namespace": st["namespace"], "pod": name}
                self._push_metric("acs_k8s", "pod.cpu.utilization", dims, st["cpu_pct"], ts_ms)
                self._push_metric("acs_k8s", "pod.memory.utilization", dims, st["mem_pct"], ts_ms)
                self._push_metric("acs_k8s", "pod.restart_count", dims, st["restart_count"], ts_ms)
            elif st["kind"] == "rds":
                dims = {"userId": W.USER_ID, "instanceId": name}
                self._push_metric("acs_rds_dashboard", "ConnectionUsage", dims, st["conn_pct"], ts_ms)
                self._push_metric("acs_rds_dashboard", "MemoryUsage", dims, st["mem_pct"], ts_ms)
                self._push_metric("acs_rds_dashboard", "CpuUsage", dims, st["cpu_pct"], ts_ms)
                self._push_metric("acs_rds_dashboard", "DiskUsage", dims, st["disk_pct"], ts_ms)
                self._push_metric("acs_rds_dashboard", "IOPSUsage", dims, st["iops_pct"], ts_ms)
            else:
                dims = {"userId": W.USER_ID, "instanceId": name}
                self._push_metric("acs_kvstore", "CpuUsage", dims, st["cpu_pct"], ts_ms)
                self._push_metric("acs_kvstore", "MemoryUsage", dims, st["mem_pct"], ts_ms)
                self._push_metric("acs_kvstore", "ConnectionUsage", dims, st["conn_pct"], ts_ms)

        # 节点级：由该节点上 Pod 水位聚合
        for node in W.NODES:
            pods = [i for i in self.instances.values()
                    if i["kind"] == "pod" and i.get("node") == node["name"]]
            dims = {"userId": W.USER_ID, "cluster": W.CLUSTER, "node": node["name"]}
            cpu = sum(p["cpu_pct"] for p in pods) / max(1, len(pods)) * 0.75
            mem = sum(p["mem_pct"] for p in pods) / max(1, len(pods)) * 0.8
            self._push_metric("acs_k8s", "node.cpu.utilization", dims, _clamp(cpu), ts_ms)
            self._push_metric("acs_k8s", "node.memory.utilization", dims, _clamp(mem), ts_ms)
            self._push_metric("acs_k8s", "node.disk.utilization", dims,
                              _clamp(35 + (self.rng.random() - 0.5) * 2), ts_ms)

        pod_count = sum(1 for i in self.instances.values() if i["kind"] == "pod")
        self._push_metric("acs_k8s", "cluster.pod.count",
                          {"userId": W.USER_ID, "cluster": W.CLUSTER}, pod_count, ts_ms)
        self._push_metric("acs_k8s", "namespace.cpu.oversale_rate",
                          {"userId": W.USER_ID, "cluster": W.CLUSTER, "namespace": "default"},
                          self.current_oversale_pct(), ts_ms)

    def current_oversale_pct(self) -> float:
        """按当前（可被治理动作修改的）service_state 复算超卖率。"""
        with self.lock:
            return self._oversale_pct_locked()

    def _oversale_pct_locked(self) -> float:
        total_limit_m = 0.0
        for svc, st in self.service_state.items():
            if W.SERVICES[svc]["namespace"] != "default":
                continue
            total_limit_m += W.parse_cpu_m(st["limits"].get("cpu")) * st["replicas"]
        total_alloc_m = W.parse_cpu_m(W.NODE_ALLOCATABLE["cpu"]) * len(W.NODES)
        return total_limit_m / total_alloc_m * 100 if total_alloc_m else 0.0

    def _prune_buffers(self):
        """按窗口裁剪日志/trace 缓冲（CMS 由 deque maxlen 天然环形）。"""
        cutoff_s = self.tick_ts_ms // 1000 - config.LOG_WINDOW_MINUTES * 60
        for store, buf in self.log_buffer.items():
            while buf and int(buf[0]["__time__"]) < cutoff_s:
                buf.popleft()
        while self.trace_buffer and self.trace_buffer[0]["__time__"] < cutoff_s:
            self.trace_buffer.popleft()

    # ------------------------------------------------------------------ 对外查询

    # --- 缓冲快照：给渲染器用 ---
    # 渲染器要按时间窗过滤上千条记录，若整段持锁会把 tick 卡住；而直接遍历原始
    # deque 又会撞上 tick 的 popleft（实测 RuntimeError）。折中：持锁只做一次浅拷贝，
    # 拷贝出来的 list 交给调用方在锁外慢慢过滤。
    # 浅拷贝够用：单条记录写入后不再被修改，只有容器本身会被增删。

    def snapshot_logs(self, logstore: str) -> list:
        with self.lock:
            return list(self.log_buffer.get(logstore, ()))

    def snapshot_traces(self) -> list:
        with self.lock:
            return list(self.trace_buffer)

    def snapshot_metrics(self) -> tuple:
        """返回 (metric_buffer 快照, _metric_meta 快照)。

        两者必须在【同一把锁内】一起取：分两次取的话，中间可能新增一个 series，
        导致 buffer 里有 key 而 meta 里没有 → 渲染时 KeyError。
        """
        with self.lock:
            return ({k: list(v) for k, v in self.metric_buffer.items()},
                    {k: dict(v) for k, v in self._metric_meta.items()})

    def realtime_snapshot(self, service: str = None, instance: str = None) -> dict:
        """/realtime/metrics：直接读当前拍状态，含水位/容量/带宽三组字段。"""
        with self.lock:
            return self._realtime_snapshot_locked(service, instance)

    def _realtime_snapshot_locked(self, service: str = None, instance: str = None) -> dict:
        items = []
        for name, st in self.instances.items():
            if service and st["service"] != service:
                continue
            if instance and name != instance:
                continue
            if st["kind"] == "pod":
                svc_state = self.service_state[st["service"]]
                replicas = svc_state["replicas"]
                capacity_pct = round(max(st["cpu_pct"], st["mem_pct"]), 2)
                max_conn = None
            else:
                replicas = 1
                max_conn = self.datastore_state[st["service"]]["max_connections"]
                capacity_pct = round(st["conn_pct"], 2)
            bw_total = st["bandwidth_in_mbps"] + st["bandwidth_out_mbps"]
            items.append({
                "service": st["service"], "instance": name, "kind": st["kind"],
                "node": st.get("node"), "zone": st.get("zone"), "ip": st.get("ip"),
                "water_level": {"cpu_pct": round(st["cpu_pct"], 2),
                                "mem_pct": round(st["mem_pct"], 2),
                                "conn_pct": round(st["conn_pct"], 2)},
                "capacity": {"replicas": replicas, "max_conn": max_conn,
                             "capacity_pct": capacity_pct},
                "bandwidth": {"in_mbps": st["bandwidth_in_mbps"], "out_mbps": st["bandwidth_out_mbps"],
                              "usage_pct": round(bw_total / st.get("bandwidth_capacity_mbps", 1) * 100, 2)},
                "status": st["status"],
            })
        return {"ts": self.tick_ts_ms, "tick": self.tick_no, "items": items}

    def world_status(self) -> dict:
        with self.lock:
            return self._world_status_locked()

    def _world_status_locked(self) -> dict:
        return {
            "world_version": self.world_version, "tick": self.tick_no, "tick_ts": self.tick_ts_ms,
            "entry_rps": config.ENTRY_RPS, "trace_sample_rate": config.TRACE_SAMPLE_RATE,
            "services": [
                {"name": svc, "kind": W.SERVICES[svc]["kind"], "namespace": W.SERVICES[svc]["namespace"],
                 "replicas": st["replicas"], "healthy_replicas": st["healthy_replicas"],
                 "probes": st["probes"], "pdb": st["pdb"], "zone_affinity": st["zone_affinity"],
                 "requests": st["requests"], "limits": st["limits"]}
                for svc, st in self.service_state.items()
            ],
            "datastores": [
                {"name": n, "kind": W.DATASTORES[n]["kind"],
                 "max_connections": st["max_connections"],
                 "slow_query_tables": st["slow_query_tables"], "indexed_tables": st["indexed_tables"]}
                for n, st in self.datastore_state.items()
            ],
            "instances": [
                {"instance": n, "service": s["service"], "kind": s["kind"], "status": s["status"],
                 "cpu_pct": round(s["cpu_pct"], 1), "mem_pct": round(s["mem_pct"], 1),
                 "conn_pct": round(s["conn_pct"], 1), "healthy": s.get("healthy", True)}
                for n, s in self.instances.items()
            ],
            "edges": [{"source": s, "target": t} for s, t in W.EDGES],
            # backend 一并暴露：接口风险（API-001）的 resource_ref 是接口路径，
            # 而故障场景的根因标注是服务名 —— 没有这个映射就无法判定
            # "报出的接口是否属于故障服务"，评测会把正确结果误判成未命中。
            "apis": [{"api": k, "backend": W.APIS_BY_KEY[k]["backend"]
                      if k in getattr(W, "APIS_BY_KEY", {}) else None,
                      "qps": round(v["qps"], 2), "p99_ms": round(v["p99_ms"], 1),
                      "error_rate": round(v["error_rate"], 4)} for k, v in self.apis.items()],
            "active_faults": list(self.active_faults),
            "recent_actions": list(self.recent_actions),
            "cpu_oversale_pct": round(self._oversale_pct_locked(), 2),
            "audit": list(self.audit)[-10:],
            "buffer_stats": {
                "metric_series": len(self.metric_buffer),
                "ingress_logs": len(self.log_buffer["nginx-ingress"]),
                "app_logs": len(self.log_buffer["app-log"]),
                "slow_logs": len(self.log_buffer["rds_slow_log"]),
                "k8s_events": len(self.log_buffer["k8s-events"]),
                "trace_spans": len(self.trace_buffer),
            },
        }


_engine: WorldEngine = None


def get_engine() -> WorldEngine:
    global _engine
    if _engine is None:
        check = W.self_check()
        if not check["ok"]:
            raise RuntimeError(f"世界观自检失败: {check['errors']}")
        _engine = WorldEngine()
    return _engine
