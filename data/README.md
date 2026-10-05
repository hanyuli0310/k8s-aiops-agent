# 自洽微型世界 Mock 数据集

本数据集模拟一个生产 K8s 集群（prod-cluster-01 / cn-hangzhou）的完整可观测数据切片：所有数据围绕同一套服务拓扑与时间线生成、彼此自洽，其中预埋了 **11 个风险**（标准答案见 `data/world_manifest.json` 的 `risk_ground_truth`）。指标数据对齐阿里云 CMS 格式，日志/事件/Trace/慢日志对齐阿里云 SLS 格式。纯数据交付，不含生成代码。

## 目录结构

| 文件 | 内容 | 规模 |
| --- | --- | --- |
| `README.md` | 本索引 | — |
| `世界观Mock数据说明.md` | 数据集完整说明（含逐条风险验证方法） | 45.9 KB |
| `data/k8s_resources.json` | K8s 资源快照（Deployment/Pod/Service 等 35 个资源） | 62.2 KB |
| `data/cms_metrics.json` | CMS 监控指标（16 组时序，60s 粒度，60 分钟窗口） | 778.4 KB |
| `data/sls_ingress_logs.jsonl` | SLS Ingress 访问日志 | 11939 行 |
| `data/sls_k8s_events.jsonl` | SLS K8s 事件 | 40 行 |
| `data/sls_app_logs.jsonl` | SLS 应用日志 | 395 行 |
| `data/sls_rds_slowlog.jsonl` | SLS RDS 慢日志 | 50 行 |
| `data/sls_trace_spans.jsonl` | SLS Trace Span | 9246 行 |
| `data/world_manifest.json` | 世界清单：meta、11 个风险标准答案、拓扑与 API 基线 | 14.1 KB |

## 从哪里开始

1. 先通读《世界观Mock数据说明.md》，了解世界观设定、服务拓扑与各数据源格式；
2. 打开 `data/world_manifest.json`，其中 `risk_ground_truth` 是 11 个预埋风险的标准答案（含风险类型、涉及对象与证据位置）；
3. 按说明文档第 4 节的方法，在各数据文件中逐条验证这 11 个风险。

## 数据版本

来自 `world_manifest.json` 的 `meta`：

- generator_version：`world-1.0.0`
- seed：`42`
- end_ts_ms：`1785757800000`（数据时间线终点，2026-08-03T11:50:00Z）
- CMS 窗口：1785754200000 ~ 1785757800000（60 分钟，period 60s）；日志窗口：1785756000 ~ 1785757800（30 分钟）
