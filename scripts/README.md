# 启停脚本与部署踩坑清单

`scripts/start_all.sh` / `scripts/stop_all.sh` 用于在一台机器上跑起 live 模式全链路
（mock_server:9001 + data_collector + backend:8000 + frontend:5173），本地和服务器通用。

```bash
bash scripts/start_all.sh                          # 前端对外开放（0.0.0.0:5173）
FRONTEND_HOST=127.0.0.1 bash scripts/start_all.sh  # 前端只监听本机，配 SSH 隧道用
LOG_DIR=/root/logs bash scripts/start_all.sh       # 日志与 pid 目录，默认 /tmp/aiops
bash scripts/stop_all.sh                           # 停止需用同一个 LOG_DIR
```

`start_all.sh` 可重复执行：已在运行的服务会跳过，可用来补起单个挂掉的进程。

## 部署到服务器

实测环境：阿里云 ECS Ubuntu 24.04、2C4G、Python 3.12、Node 18，裸进程跑，不需要 docker。

**1. 同步代码与配置**——用 rsync 同步工作区而不是 git clone：`backend/.env`、
`data_collector/.env` 被 gitignore，仓库里没有，只能从工作区带过去。下面的 exclude
列表刻意不排除 `.env`：

```bash
rsync -az --delete \
  --exclude '.git/' --exclude 'node_modules/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '.qoder/' --exclude '.DS_Store' \
  --exclude '*.pyc' --exclude '*.db' --exclude '*.log' \
  ./ <host>:/root/k8s-aiops-agent/

# 核对 .env 有没有传到
md5 backend/.env data_collector/.env
ssh <host> 'md5sum /root/k8s-aiops-agent/backend/.env /root/k8s-aiops-agent/data_collector/.env'
```

> **每套部署必须用独立的 `DB_NAME`**，见下方坑 2。

**2. 装依赖**

```bash
cd /root/k8s-aiops-agent/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt \
  -r ../mock_server/requirements.txt -r ../data_collector/requirements.txt
cd ../frontend && npm install
```

**3. 启动并对外开放**

```bash
LOG_DIR=/root/logs bash scripts/start_all.sh
```

对外只需放行 **5173** 一个端口：阿里云安全组 → 入方向 → 允许 / TCP / `5173/5173` /
授权对象填同事出口 IP（各自 `curl ifconfig.me` 获取），或临时 `0.0.0.0/0`。
访问 `http://<公网IP>:5173`。

> ⚠️ 用 `0.0.0.0/0` 时公网任何人都能调 `/api/chat`（消耗百炼 token）、`/api/fault/inject`、
> `/api/governance/execute`。**演示结束立刻删掉这条规则。**

---

## 实测踩过的坑

### 坑 1：不要把 backend 和 mock_server 绑到 0.0.0.0

`start_all.sh` 让 mock_server 与 backend 只监听 `127.0.0.1`，只有前端按
`FRONTEND_HOST` 对外暴露。外部对 `/api` 的请求由 vite 在机器内部代理到
`127.0.0.1:8000`，所以对外只放行 5173 就够。

如果 backend 也绑 0.0.0.0，一旦安全组放行就把带 LLM Key 的 Agent 入口和 mock 的
`/control/inject_fault`、`/control/actions` 控制面一起暴露到公网，任何人都能注入故障、
改动世界状态。

### 坑 2：多套部署共用一个 `DB_NAME` → 风险面板在 7 条与 0 条之间反复跳

最隐蔽的一个。本地开发机和服务器各跑一套 live，`.env` 里 `DB_NAME` 相同时：两台机器
各有自己的 mock 世界（世界状态、治理历史不同），两个 collector 每 60s 各自对同一张
`k8s_resources` 做**全量替换**，互相覆盖。

现象是配置类规则（CAP-001/002/003、HA-001~004）的 7 条预埋缺陷集体在 `open` 与
`resolved` 之间翻转，前端「新增 N 条」跟着乱跳，看起来像扫描逻辑有 bug，实际上代码没问题。

排查方法——先高频探测行数，再看连接来源：

```sql
SELECT COUNT(*) FROM k8s_resources;   -- 每 0.2s 一次；出现两个不同的稳定值 = 有第二个写入方
SELECT host, db FROM information_schema.processlist;   -- 直接看客户端 IP
```

**每人 / 每套部署用独立库**（如 `ai_devops_cmdb_lin` / `ai_devops_cmdb_lin_ecs`），
或演示期间只保留一套在跑。

### 坑 3：SSH 隧道只绑 IPv4 时浏览器打不开，但 curl 是通的

macOS 上 `localhost` 优先解析成 IPv6 `::1`。`ssh -L 127.0.0.1:15173:...` 只监听 IPv4，
浏览器连 `::1` 直接被拒；而终端里 `curl localhost:15173` 反而成功（curl 会回退到 IPv4），
很容易误判成"服务挂了"。两个协议栈都要转发：

```bash
ssh -N -L '127.0.0.1:15173:127.0.0.1:5173' -L '[::1]:15173:127.0.0.1:5173' <host>
```

引号是必要的，zsh 会把 `[::1]` 当通配符吃掉。本地端口另选（如 15173）可避开本机已在
跑的 5173。

### 坑 4：停服务不要用 `pkill -f`

- `pkill -f <项目名>` 会把**命令行里含该字符串的 ssh 会话自己**也杀掉，命令执行到一半就断连
- `pkill -f 'uvicorn app.main:app'`、`pkill -f vite` 会杀掉队友在同一台机器上跑的进程
- `pkill -P <npm的pid>` 只杀直接子进程，孙进程 vite 变孤儿继续占着 5173，
  新实例静默退到 5174，日志里只有一句 `Port 5173 is in use, trying another one...`

所以 `start_all.sh` 用 `setsid` 让每个服务独立成进程组，`stop_all.sh` 按 pid 文件
`kill -TERM -<pgid>` 整组回收。

### 坑 5：`package-lock.json` 指向内网 registry，公网机器 npm install 必挂

本地用 npm 装过依赖后，lock 文件里 500 多处 `resolved` 都指向
`<内部 npm 源>`。npm 优先用 lock 里的地址，公网 ECS 访问不到，
报 `ERR_SOCKET_TIMEOUT`——此时改 `npm config set registry` 是**没用的**。

```bash
cd frontend
cp package-lock.json package-lock.json.orig
sed -i 's#<内部 npm 源>#registry.npmmirror.com#g' package-lock.json
npm install
```

### 坑 6：同事打不开公网地址，八成不是服务器问题

先用一个与自己不同的网络出口确认服务端真的全网可达，再让同事跑：

```bash
curl -sv --max-time 10 http://<公网IP>:5173/ 2>&1 | head -5
```

- 出现 `Connected to ...` → 网络通，是浏览器问题：多半漏了 `http://`（被当搜索词），
  或被自动升级成 `https://`（服务端没有 TLS）
- 超时 / refused → 对方网络出口拦了非标端口，换手机热点可立刻验证
