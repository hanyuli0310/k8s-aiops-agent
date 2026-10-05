---
name: fault_diagnosis
description: 故障根因定位的五步排查法——从症状横向对比到 Trace 下钻，输出带证据链的根因结论
when_to_use: 用户报告某接口慢/报错/超时，或需要定位一条已知风险的根因时
---

# Skill: 故障根因定位（五步排查法）

## 适用
用户反馈某接口/服务慢、报错、超时（如"下单接口很慢"、"支付失败"）。

## 五步排查法（每步都必须用工具拿真实数据，严禁凭空推断）

1. **圈定病灶接口**：调 `api_perf_stats` 按接口聚合 P99/错误率，找出显著劣化的接口（对比其他接口基线）。用户说"下单"对应 POST /api/orders，"支付"对应 POST /api/pay，"登录"对应 POST /api/users/login。
2. **抓一条问题链路**：调 `query_traces`，传 api_name=病灶接口 + only_error=true，拿最慢/出错的根 span 的 trace_id。
3. **链路下钻**：调 `query_traces` 传 trace_id，逐层看每个 span 耗时占比，找出耗时占大头的 span（如某个 SQL span 占整链 80%+），记下它的 db_statement 和 error 文案。
4. **多源收口**：
   - 慢 SQL：拿 db_statement 关键词调 `query_logs`(logstore=slow) 查慢日志，看 rows_examined（百万级+rows_sent 个位数=无索引全表扫描）；
   - 错误：拿 error 文案关键词调 `query_logs`(logstore=app, level=ERROR)；
   - 资源水位：调 `query_metrics`(namespace=acs_rds_dashboard) 看 ConnectionUsage/MemoryUsage。
5. **根因结论 + 方案**：输出因果链（症状 → 直接原因 → 底层压力），给出可执行的治理动作（工具名 + 参数），请用户确认后执行。

## 结论输出格式
- 【症状】接口 X P99=?s 错误率=?%
- 【根因链】逐环节说明，每个环节附证据（数值/trace_id/日志条数）
- 【治理方案】列出具体工具调用建议（如 create_db_index orders(status, created_at)），标注"待确认后执行"

## 注意
- K8s 事件全部是 Normal 发布事件，没有故障事件——不要从事件里编造故障；
- Trace 采样率约 10%：从 trace 侧找问题请求最稳妥；
- 结论必须每个环节都有数据支撑，宁可少说不可编造。
