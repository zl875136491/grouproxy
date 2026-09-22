# Grouproxy

Grouproxy 是一个面向多地域节点的代理运维控制平面。控制面负责保存配置、订阅、策略和运行状态，并为每个节点生成签名的 Desired Bundle；每个节点上的 monitor 负责本机 `sing-box`、代理端口和 nftables 防火墙策略。

最重要的边界是：**员工流量不经过控制面**。控制面只传递配置和接收运维遥测，实际代理连接始终从员工所在网络直达本地节点。

当前实现版本：控制面和 monitor `0.6.0`，内置 sing-box `1.13.19`（Linux amd64）。

## 快速了解

### 30 秒看懂

| 组件 | 技术 | 职责 | 默认监听 |
| --- | --- | --- | --- |
| Dashboard | Next.js 15 / React 19 | 管理员登录、节点/站点、订阅、发布、遥测和审计操作 | `:80` |
| Backend | FastAPI / Python 3.12 | 控制平面 API、MongoDB 文档、签名 Bundle、后台任务和认证 | `:8000` |
| MongoDB | MongoDB | 持久化站点、节点、版本、发布、任务、遥测、审计和备份记录 | 通常 `:27017` |
| Monitor | Go | 拉取并验证 Bundle，驱动 sing-box，应用 nftables，发送心跳和遥测 | 无独立公网 API |
| sing-box | 固定 Linux amd64 二进制 | 承载每个地点的 HTTP CONNECT 代理数据面 | `:1080` |

生产部署是“五地五节点、北京控制面”：

```text
员工电脑
   │ 直接连接本地地点的 proxy.1oa.com.cn:1080
   ▼
本地节点 monitor ── 管理 sing-box + nftables ──► 上游代理节点
   │
   │ 心跳、配置拉取、ACK、遥测；不承载员工流量
   ▼
北京 Backend :8000 ◄── Dashboard :80 ◄── 管理员浏览器
   │
   ▼
MongoDB
```

### 当前网络契约

- 对外代理协议是 HTTP CONNECT，固定端口 `1080`。
- 代理没有 HTTP Basic 认证，也不安装代理凭据或 CA；访问控制默认是放行，明确配置的黑名单才会拒绝请求。
- 黑名单按节点保存，可按来源或目标匹配 `IP`、`CIDR`、域名。创建、删除规则会为受影响节点自动生成发布。
- Dashboard 由 Next.js 直接监听 `80`。Next.js 将 `/api/*`、`/healthz` 和 `/readyz` 重写到 Backend，因此生产环境可让 Backend 只服务于内网或本机。
- Monitor 只通过 `/agent/v1/*` 与 Backend 通信。Desired Bundle 除了版本和 SHA-256，还使用共享 HMAC 密钥签名。
- 节点自己拉起和重载 sing-box。生产环境不要启用仓库中的 `deploy/sing-box.service`，以免与其他产品或 monitor 管理的进程冲突。
- 当前没有 TLS/HTTPS 代理监听、`443`、mTLS、客户端证书或外部 CI/CD 部署流程。

### 主要能力

- 站点和节点管理：站点开关、节点注册、节点状态、配置同步状态和版本信息。
- 配置发布：草稿、校验、按站点/节点发布、版本 ACK、失败回滚和幂等重试。
- 订阅托管：HTTP/HTTPS 订阅地址、上传的 Clash YAML/SIP008/sing-box 文件、单个 VLESS/VMess 链接。
- 节点黑名单：来源 IP/CIDR/域名以及目标 IP/CIDR/域名；没有 allowlist 模型。
- 可观测性：心跳、连接摘要、访问日志、代理组快照、延迟探测、告警和哈希链审计。
- 账号：IT code 账号、密码登录、GQuan 验证码登录、角色管理和服务端会话。
- 备份：加密控制面备份、校验以及不修改线上数据的恢复演练。
- 员工接入资产：按环境提供 Linux、Windows 和 macOS 快捷指令下载。

## 快速开始：本地双节点测试

这是开发和验收用的同机模拟环境，不是生产拓扑。它使用已经存在的 MongoDB，不会启动、创建或重置 MongoDB 服务。

### 前置条件

- Python `3.12.x`，且版本小于 `3.13`。
- Go `1.22+`、Node.js（Docker 构建使用 Node 22）。
- `curl`、`jq`、`nc`、`openssl`、`nft`。
- 可访问的 MongoDB 测试库。测试脚本会先执行 MongoDB `ping`，失败时不会启动半套服务。
- Linux amd64 环境，以及仓库中校验过的 `singbox/sing-box`。

在项目根目录执行：

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -e 'backend[dev]'
(cd monitor && make dist)
(cd frontend && npm ci)
```

### 启动测试环境

默认认证验证码通过真实 One Login GQuan APP API 发送，因此需要获得批准的 APP Bearer token：

```bash
export GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@<host>:<port>/?authSource=admin'
export GROUPROXY_TEST_MONGODB_DATABASE='grouproxy_test'
export GROUPROXY_TEST_GQUAN_APP_TOKEN='sat_<approved-app-token>'

GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
```

脚本会在 `testenv/` 生成随机运行时密钥、节点 token、状态和日志。`testenv/` 已被 Git 忽略；不要把其中的密钥复制进仓库或工单。

测试监听关系如下：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| Backend | `127.0.0.1:8000` | 测试脚本使用；后端自身不暴露到公网 |
| Dashboard | `0.0.0.0:3000` | 同机测试控制台 |
| `codedev` 节点 | `0.0.0.0:1080` | 与生产节点相同的公开代理端口 |
| `nuc` 节点 | `0.0.0.0:18081` | 同机第二节点的唯一测试端口 |
| Clash API | `127.0.0.1:19090/19091` | 仅供对应 monitor 使用 |

可访问：

```text
控制台：http://test-proxy.1oa.com.cn:3000/
codedev 代理：http://test-proxy.1oa.com.cn:1080
nuc 代理：http://test-proxy.1oa.com.cn:18081
```

测试机需要将 `test-proxy.1oa.com.cn` 临时解析到测试机 IP，或者直接使用 `127.0.0.1:3000` 打开控制台。第二节点的 `:18081` 仅为同机模拟；真实的第二台服务器仍使用自己的 `:1080`。

### 使用隔离的验证码 stub

自动化认证回归不应依赖外部短信/登录服务。只有明确设置 stub profile 时才启用固定验证码：

```bash
GROUPROXY_TEST_GQUAN_DELIVERY_MODE=stub \
GROUPROXY_TEST_GQUAN_CODE=123456 \
GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
./scripts/verify-auth.sh
```

stub 只适用于隔离测试环境，生产环境必须使用 `app` 模式。

### 验收和停止

```bash
./scripts/verify-phase1.sh   # 站点、节点、黑名单、草稿、发布和 ACK
./scripts/verify-phase2.sh   # 订阅解析、发布、回滚、Blob 和任务幂等
./scripts/verify-phase3.sh   # 遥测、告警、审计、探测和员工接入资产
./scripts/verify-phase4.sh   # 加密备份和无损恢复演练

./scripts/testenv-down.sh    # 停止进程，保留 testenv/ 证据
```

其他本地检查：

```bash
(cd backend && ../.venv/bin/pytest)
(cd monitor && make test)
(cd frontend && npm run typecheck && npm run test:i18n)
```

## 详细架构

### 组件与代码边界

```text
source_code/grouproxy/
├── backend/       FastAPI 控制面、MongoDB 文档和业务服务
├── frontend/      Next.js 管理控制台
├── monitor/       Go 节点 agent、运行时、路由数据和 nftables
├── singbox/       固定版本 sing-box Linux amd64 二进制及校验信息
├── deploy/        systemd unit、节点安装器、员工接入脚本和模板
├── scripts/       本地测试环境、阶段验收和开发启动脚本
└── screenshots/   项目辅助截图
```

Backend 内部边界：

| 模块 | 职责 |
| --- | --- |
| `backend/main.py` | FastAPI 入口、认证依赖、管理端/agent 端路由、生命周期和后台循环 |
| `backend/app/config.py` | `GROUPROXY_*` 环境变量和默认值 |
| `backend/app/models.py` | Beanie/MongoDB 文档、TTL 索引和幂等索引 |
| `backend/app/schemas.py` | API 输入输出模型和边界校验 |
| `backend/app/services/bundles.py` | Desired Bundle 生成、版本递增和选中订阅组装 |
| `backend/app/services/subscriptions.py` | URL、上传文件、VLESS/VMess 的解析和安全校验 |
| `backend/app/services/subscription_worker.py` | 订阅刷新任务的调度、租约、重试和死信 |
| `backend/app/services/tasks.py` | 通用任务状态、租约、取消和幂等 |
| `backend/app/services/audit.py` | 敏感字段脱敏和审计哈希链 |
| `backend/app/services/alerts.py` | 节点健康、拒绝突增等告警 |
| `backend/app/services/backups.py` / `backup_worker.py` | 加密备份、保留和恢复演练 |

Monitor 内部边界：

| 模块 | 职责 |
| --- | --- |
| `monitor/cmd/monitor` | 主循环、Bundle 应用、心跳、探测和遥测采集 |
| `monitor/internal/client` | 对 Backend agent API 的 HTTP 客户端 |
| `monitor/internal/bundle` | Bundle canonical JSON、SHA-256、HMAC、过期和版本校验 |
| `monitor/internal/runtime` | sing-box 进程、配置检查、监听端口和 Clash API 健康检查 |
| `monitor/internal/firewall` | 安全生成、校验和应用 nftables 规则 |
| `monitor/internal/subscription` | 订阅 Blob 的哈希校验和 Clash/SIP008/sing-box 解析 |
| `monitor/internal/routingdata` | 内置 `geoip-cn`、`geosite-cn` 二进制规则集 |
| `monitor/internal/state` | 节点本地版本、last-good、序列号和遥测状态 |

### 两套 API 面

管理面 `/api/v1/*`：

- Dashboard 使用服务端 opaque session；已有本地验收脚本也可使用 `GROUPROXY_MANAGEMENT_TOKEN` Bearer token。
- 管理写操作受 Origin 检查和内存滑动窗口限流保护。
- 常用接口包括 `/auth/*`、`/sites`、`/nodes`、`/blacklist`、`/subscriptions`、`/config/drafts`、`/config/releases`、`/tasks`、`/logs`、`/connections/live`、`/connections/history`、`/alerts`、`/backups` 和 `/overview`。连接历史支持站点、节点、时间、来源 IP、目标、协议、出站和关键词筛选。
- 员工接入资产位于 `/access/config`、`/access/proxy.pac`、`/access/linux-setup.sh` 和 `/access/windows-setup.ps1`。

节点面 `/agent/v1/*`：

| 接口 | 方向 | 作用 |
| --- | --- | --- |
| `GET /agent/v1/desired` | Monitor → Backend | 按节点返回是否有新 Desired Bundle |
| `GET /agent/v1/blobs/{sha256}` | Monitor → Backend | 仅取当前 Bundle 引用的订阅大 Blob |
| `POST /agent/v1/heartbeat` | Monitor → Backend | 上报版本、状态、流量和待执行探测 |
| `POST /agent/v1/ack` | Monitor → Backend | 上报应用、健康检查和回滚结果 |
| `POST /agent/v1/logs` | Monitor → Backend | 上报访问日志批次 |
| `POST /agent/v1/connections` | Monitor → Backend | 上报连接和流量快照 |
| `POST /agent/v1/proxy-config` | Monitor → Backend | 上报已脱敏的 Clash 代理组快照 |
| `POST /agent/v1/probes` | Monitor → Backend | 上报通过各上游的探测结果 |

`/healthz` 只表示进程存活并返回 API 版本；`/readyz` 还会 ping MongoDB，适合作为服务就绪检查。

### 配置发布和回滚流程

```text
管理员修改订阅/黑名单/选择
        │
        ▼
Backend 校验输入，生成不可变版本或草稿
        │
        ▼
按 site/node 生成 DesiredRelease，递增 desired_version
并计算 bundle_hash + HMAC
        │
        ▼
Monitor 心跳/轮询 desired
        │
        ├─ 校验 node_id、版本、防重放、有效期、SHA-256、HMAC、最低 monitor 版本
        ├─ 校验订阅内容；必要时按 hash 拉取 Blob
        ├─ 写入本地 CN 路由规则，渲染 sing-box candidate
        ├─ 执行 sing-box check 和 nftables check
        ├─ 按 firewall_mode 应用 nftables，重载/启动 sing-box
        └─ 在健康窗口内检查进程、代理端口和 Clash API
        │
        ├─ 成功：持久化 last-good，ACK succeeded，节点变为 in_sync
        └─ 失败：恢复 last-good 配置和防火墙，ACK rolled_back/failed
```

每个成功的候选配置会写入节点的状态目录，包括 `last-good.json`、`last-good-bundle.json`、`last-good-nft.json` 和版本文件。健康窗口没有通过前不会覆盖 last-good。源域名黑名单会在节点上解析成具体 IP，并与 last-good 一起保存，以便短暂 DNS 故障时仍能回滚。

### Desired Bundle 的内容原则

- 固定 `listen.http_port: 1080`，生产节点不接受任意端口。
- 只携带当前节点适用的 `blacklist` 条目，不携带其他节点规则。
- 选中订阅较小时可内嵌内容；超过 `GROUPROXY_SUBSCRIPTION_INLINE_MAX_BYTES` 时只携带 hash 和受保护的 Blob 引用。
- 订阅 Blob 只有在当前节点的 Bundle 明确引用对应 hash 时才能读取，节点 token 不能用来遍历整个订阅库。
- monitor 只使用本地内置 `geoip-cn.srs` 和 `geosite-cn.srs`，运行时不下载路由数据。
- 旧的 allowlist、代理认证、跨站规则和旧目的地策略字段会在 Backend/Monitor 启动迁移时清理，不能通过历史状态重新启用。

### 订阅模型

支持三类来源：

1. HTTP/HTTPS 订阅地址：由控制面拉取，节点不会直接访问供应商 URL。
2. 上传文件：支持 Clash YAML、SIP008 和 sing-box outbound 文档；上传后不可刷新。
3. 单节点链接：支持 VLESS（包括 Reality Vision）和标准 Base64 JSON VMess；导入后转为一个不可变 sing-box outbound。

HTTP 来源的刷新任务具备以下约束：

- 检查 URL、DNS 结果和每次重定向，拒绝本地或非公网地址，限制响应大小和重定向次数。
- 同一来源同时只能有一个 active refresh task；MongoDB partial unique index 负责并发保护。
- 任务支持租约恢复、指数退避、取消和 dead-letter 状态。
- HTTPS 上游当前允许使用不受信任的证书（代码使用 `verify=False`）；这不是 Grouproxy 的 HTTPS 监听能力，生产上应优先使用可信上游。

版本本身不可变。发布操作只把某个版本绑定到一个或多个站点，站点的当前选择和上一次选择都会被记录；发布和回滚都走普通的 release、monitor ACK 和健康检查流程。带有相同 `Idempotency-Key` 的重试会返回原有任务/发布，不会覆盖回滚历史。

### 遥测、告警和审计

Monitor 周期性发送：

- 心跳：版本、应用版本、配置/服务状态、连接数、累计流量和速率。
- 访问日志：拒绝记录完整保留，允许记录约 1% 采样；查询参数、Cookie、Authorization 等不进入控制面存储。
- 连接快照：活动连接、上下行流量、来源/目标摘要和受限的活动连接投影。
- 代理配置：只保存 Clash `/proxies` 的组名、选择状态、类型、UDP 和延迟历史，不保存服务器地址、凭据或原始订阅内容。
- 探测结果：按任务选择上游，经本地代理访问目标后回传延迟和错误分类。

控制面以节点和遥测类型维护单调序列、批次幂等和过期 TTL。Backend 不可达时 Monitor 将批次写入有大小上限的本地 spool，恢复后重放；拒绝日志优先于普通采样数据。后台观测循环每 15 秒刷新节点存活和告警，漏掉约 45 秒心跳的节点会标为 offline。

审计事件写入 MongoDB 时包含前后值、请求 ID、操作者、来源 IP、结果和前一事件哈希。敏感字段会脱敏，`/api/v1/audit/verify` 可验证保留窗口内的哈希链。审计记录默认有 TTL。

## 正式部署：北京控制面 + 五地节点

正式部署的完整逐条命令也见 [`deploy/README.md`](deploy/README.md)。本节说明拓扑、关键步骤和必须遵守的边界。

### 拓扑和端口

| 地点 | 运行组件 | 对外或跨机端口 |
| --- | --- | --- |
| 北京 | MongoDB、Backend、Dashboard、monitor、sing-box | `80` 控制台，`1080` 代理，`8000` 仅给节点 monitor |
| 天津 | monitor、sing-box | `1080` 代理 |
| 昆山 | monitor、sing-box | `1080` 代理 |
| 深圳 | monitor、sing-box | `1080` 代理 |
| 杭州 | monitor、sing-box | `1080` 代理 |

北京以外不要安装 Dashboard。MongoDB `27017` 只绑定北京本机；Backend `8000` 仅允许五个节点 IP 访问。每台机器的 Clash API 必须是 loopback，例如 `127.0.0.1:9090`，端口冲突时使用其他 loopback 端口，绝不能监听 `0.0.0.0`。

### 网络和 DNS

代理域名是 `proxy.1oa.com.cn`，员工脚本使用：

```text
http://proxy.1oa.com.cn:1080
```

DNS 不按端口区分，因此需要拆分解析策略：

1. 各地办公网将 `proxy.1oa.com.cn` 解析到本地节点 IP，使员工流量留在本地。
2. 管理员访问控制台时，`proxy.1oa.com.cn` 的 `:80` 必须指向北京；也可以使用北京 IP 或单独的管理解析名。
3. 天津、昆山、深圳、杭州到北京的 TCP `8000` 必须互通，且只允许这些节点来源。
4. `:1080` 应由节点防火墙控制来源；无黑名单时必须 fail-open，不能因为空策略而意外封死代理。

### 1. 准备北京主机和目录

所有节点建议使用统一目录和 `grouproxy` 系统用户：

```text
/opt/grouproxy/src          完整仓库
/opt/grouproxy/venv         北京 Backend Python 虚拟环境
/opt/grouproxy/dashboard    北京 Next.js standalone 构建产物
/opt/grouproxy/bin          grouproxy-monitor、sing-box
/opt/grouproxy/etc          backend.env、monitor.yaml、node.token
/opt/grouproxy/var          monitor 状态、last-good、sing-box.json、备份
```

北京必须保留完整仓库布局，因为 Backend 的员工脚本读取逻辑会从源码树旁的 `deploy/` 读取文件：

```bash
sudo groupadd --system grouproxy 2>/dev/null || true
sudo useradd --system --gid grouproxy --home-dir /opt/grouproxy \
  --shell /usr/sbin/nologin grouproxy 2>/dev/null || true
sudo install -d -m 0755 /opt/grouproxy/bin /opt/grouproxy/dashboard
sudo install -d -o grouproxy -g grouproxy -m 0750 \
  /opt/grouproxy/etc /opt/grouproxy/var
sudo git clone <repository-url> /opt/grouproxy/src
```

安装 Python 3.12、Node.js、nftables 和已有 MongoDB。生产部署使用仓库中已固定的 sing-box 二进制；其 SHA-256 为：

```text
7e9dcd7239c49478a576d79f272751e5ed1c2aba7cc08ab1b2bd69c00c904ba1
```

### 2. 准备 MongoDB

可以使用北京已有 MongoDB，不需要由本仓库创建容器或数据目录。建议使用独立数据库，例如 `grouproxy`，连接串带 `authSource=admin`：

```text
mongodb://<user>:<password>@127.0.0.1:27017/?authSource=admin
```

Backend 首次连接时会 ping MongoDB、执行历史策略迁移、清理已废弃字段、准备遥测幂等索引并初始化 Beanie 文档模型。该数据库不要同时给其他应用使用。

### 3. 配置 Backend

复制模板并只让服务账号可读：

```bash
sudo cp /opt/grouproxy/src/deploy/backend.env.example \
  /opt/grouproxy/etc/backend.env
sudo chmod 0640 /opt/grouproxy/etc/backend.env
sudo chown grouproxy:grouproxy /opt/grouproxy/etc/backend.env
sudoedit /opt/grouproxy/etc/backend.env
```

生产至少需要设置：

```dotenv
GROUPROXY_ENVIRONMENT=production
GROUPROXY_MONGODB_URL=mongodb://<user>:<password>@127.0.0.1:27017/?authSource=admin
GROUPROXY_MONGODB_DATABASE=grouproxy
GROUPROXY_HOST=0.0.0.0
GROUPROXY_PORT=8000
GROUPROXY_BACKEND_PUBLIC_URL=http://127.0.0.1:8000
GROUPROXY_BUNDLE_HMAC_SECRET=<至少32字节的随机值>
GROUPROXY_ADMIN_USERNAME=zhangle
GROUPROXY_ADMIN_PASSWORD=<至少12字符的强密码>
GROUPROXY_MANAGEMENT_TOKEN=<至少32字节的随机值>
GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true
GROUPROXY_SEED_DEFAULT_SITES=true
GROUPROXY_CORS_ALLOWED_ORIGINS=
GROUPROXY_GQUAN_DELIVERY_MODE=app
GROUPROXY_GQUAN_APP_TOKEN=sat_<approved-app-token>
```

说明：

- `GROUPROXY_BUNDLE_HMAC_SECRET` 必须与所有节点 `monitor.yaml` 的 `hmac_secret` 完全相同。
- `GROUPROXY_ADMIN_USERNAME` 默认应为根管理员 `zhangle`。根账号不能停用、降级或再创建第二个 root；其他账号可在控制台提升为 admin。
- 当前正式拓扑是 HTTP，因此必须显式设置 `GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true`。如果后续改用 HTTPS agent 通道，应重新设计证书和网络配置，不要只删除这个变量。
- 同源 Dashboard 部署时 `GROUPROXY_CORS_ALLOWED_ORIGINS` 留空。开发或跨源访问时才填写明确的允许来源。
- GQuan APP token 只放在受保护的运行时环境文件或密钥管理系统中，不得提交、写入审计详情或返回浏览器。
- 备份建议配置 `GROUPROXY_BACKUP_DIRECTORY` 和稳定的 `GROUPROXY_BACKUP_ENCRYPTION_KEY`；自动备份默认关闭，启用前应确认目录权限和异机存储策略。

### 4. 安装和启动 Backend

```bash
sudo python3.12 -m venv /opt/grouproxy/venv
sudo /opt/grouproxy/venv/bin/pip install -e /opt/grouproxy/src/backend
sudo install -m 0644 /opt/grouproxy/src/deploy/grouproxy-backend.service \
  /etc/systemd/system/grouproxy-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now grouproxy-backend.service

curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

`healthz` 应返回 API 版本 `0.6.0`，`readyz` 成功表示 MongoDB 已就绪。确认 `8000` 监听在 `0.0.0.0` 后，用主机防火墙限制来源；不要把 MongoDB 端口暴露给各地节点。

### 5. 构建和启动 Dashboard（仅北京）

`GROUPROXY_BACKEND_API_URL` 是构建期写入 Next.js rewrite 的地址，正式环境指向北京本机 Backend：

```bash
cd /opt/grouproxy/src/frontend
sudo -H npm ci
sudo -H env GROUPROXY_BACKEND_API_URL=http://127.0.0.1:8000 npm run build

sudo rm -rf /opt/grouproxy/dashboard
sudo install -d /opt/grouproxy/dashboard
sudo cp -a /opt/grouproxy/src/frontend/.next/standalone/. \
  /opt/grouproxy/dashboard/
sudo chmod -R a+rX /opt/grouproxy/dashboard
sudo install -m 0644 /opt/grouproxy/src/deploy/grouproxy-dashboard.service \
  /etc/systemd/system/grouproxy-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now grouproxy-dashboard.service

curl -fsS http://127.0.0.1/healthz
```

Dashboard systemd unit 使用 `DynamicUser` 和 `CAP_NET_BIND_SERVICE` 绑定 `80`。若 `80` 已被 nginx 或其他服务占用，应先处理入口冲突；不要把 Dashboard 安装到外地节点。

登录入口是 `http://proxy.1oa.com.cn/`，根 IT code 是 `zhangle`，初次创建时密码来自 `GROUPROXY_ADMIN_PASSWORD`。生产环境不会在每次启动时覆盖已有密码。

### 6. 创建站点、注册节点并保存一次性 token

启用 `GROUPROXY_SEED_DEFAULT_SITES=true` 后，Backend 会准备五个默认站点。建议在控制台将显示名改为北京、天津、昆山、深圳、杭州。推荐固定的 `agent_id`：

| 默认 slug | 显示名 | `agent_id` |
| --- | --- | --- |
| `north` | 北京 | `beijing` |
| `east` | 天津 | `tianjin` |
| `central` | 昆山 | `kunshan` |
| `south` | 深圳 | `shenzhen` |
| `west` | 杭州 | `hangzhou` |

`agent_id` 写入后不可修改，只能修改节点显示名。注册节点时，创建响应中的 `agent_token` 只返回一次，应立即写入目标机：

```bash
API=http://127.0.0.1:8000
TOKEN='<GROUPROXY_MANAGEMENT_TOKEN>'
AUTH="Authorization: Bearer ${TOKEN}"

curl -fsS -H "$AUTH" "$API/api/v1/sites" | jq '.[] | {id,slug,name}'

curl -fsS -X POST "$API/api/v1/nodes" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"site_id":"<北京site_id>","name":"beijing","agent_id":"beijing","advertise_ip":"<北京IP>"}' \
  | tee /tmp/node-beijing.json

jq -r .agent_token /tmp/node-beijing.json \
  | sudo -u grouproxy tee /opt/grouproxy/etc/node.token >/dev/null
sudo chmod 0600 /opt/grouproxy/etc/node.token
rm -f /tmp/node-beijing.json
```

四个外地节点使用相同流程，分别绑定对应站点。不要尝试通过 API 再次读取或隐式轮换已有 token；丢失 token 时应按运维流程显式处理节点凭据。

### 7. 启动北京 monitor

```bash
sudo install -m 0755 /opt/grouproxy/src/monitor/dist/grouproxy-monitor-linux-amd64 \
  /opt/grouproxy/bin/grouproxy-monitor
sudo install -m 0755 /opt/grouproxy/src/singbox/sing-box \
  /opt/grouproxy/bin/sing-box
sudo cp /opt/grouproxy/src/deploy/monitor.yaml.example \
  /opt/grouproxy/etc/monitor.yaml
sudoedit /opt/grouproxy/etc/monitor.yaml
```

北京节点至少修改为：

```yaml
backend_url: "http://127.0.0.1:8000"
node_id: "beijing"
token_file: "/opt/grouproxy/etc/node.token"
state_dir: "/opt/grouproxy/var"
singbox_bin: "/opt/grouproxy/bin/sing-box"
singbox_config: "/opt/grouproxy/var/sing-box.json"
listen_port: 1080
firewall_mode: apply
run_singbox: true
hmac_secret: "<与 Backend 完全相同的值>"
allow_insecure_http: true
clash_api_listen: "127.0.0.1:9090"
```

设置文件权限并先验证：

```bash
sudo chown grouproxy:grouproxy /opt/grouproxy/etc/monitor.yaml \
  /opt/grouproxy/etc/node.token
sudo chmod 0640 /opt/grouproxy/etc/monitor.yaml
sudo -u grouproxy /opt/grouproxy/bin/grouproxy-monitor \
  -config /opt/grouproxy/etc/monitor.yaml -validate
sudo install -m 0644 /opt/grouproxy/src/deploy/grouproxy-monitor.service \
  /etc/systemd/system/grouproxy-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now grouproxy-monitor.service
```

`-validate` 只校验配置、token、路径和 sing-box 完整性，不访问 Backend，也不会改防火墙或启动 sing-box。不要执行 `systemctl enable sing-box.service`，monitor 已经拥有 sing-box 子进程生命周期。

### 8. 安装天津、昆山、深圳、杭州节点

先在北京注册节点并取得对应的一次性 token，再从北京源码目录运行节点安装器：

```bash
cd /opt/grouproxy/src
NUC_SSH_USER=operator NUC_SSH_KEY=/path/to/key \
  ./deploy/install-node.sh <目标节点IP>
```

安装器只复制 `grouproxy-monitor`、`sing-box` 和 `grouproxy-monitor.service`，创建 `/opt/grouproxy/{bin,etc,var,run}` 目录，并在启用服务前执行 `-validate`。它不会安装 Dashboard，也不会覆盖 `/etc/systemd/system/sing-box.service`。

目标节点的 `monitor.yaml` 需要：

```yaml
backend_url: "http://<北京IP>:8000"
node_id: "<注册时的agent_id>"
token_file: "/opt/grouproxy/etc/node.token"
state_dir: "/opt/grouproxy/var"
singbox_bin: "/opt/grouproxy/bin/sing-box"
singbox_config: "/opt/grouproxy/var/sing-box.json"
listen_port: 1080
firewall_mode: apply
run_singbox: true
hmac_secret: "<与北京 Backend 相同的值>"
allow_insecure_http: true
clash_api_listen: "127.0.0.1:9090"
```

若本机 `9090` 已被占用，改为其他 loopback 端口，例如 `127.0.0.1:19091`。把 token 放在 `/opt/grouproxy/etc/node.token` 后执行：

```bash
sudo -u grouproxy /opt/grouproxy/bin/grouproxy-monitor \
  -config /opt/grouproxy/etc/monitor.yaml -validate
sudo systemctl enable --now grouproxy-monitor.service
```

### 9. 正式环境验收

北京控制面：

```bash
curl -fsS http://127.0.0.1/healthz
curl -fsS http://127.0.0.1:8000/readyz
ss -ltnp | grep -E ':80 |:8000 |:1080 '
```

每个节点：

```bash
systemctl is-active grouproxy-monitor.service
ss -ltnp | grep ':1080 '
curl -sS --max-time 8 -x http://127.0.0.1:1080 \
  -o /dev/null -w '%{http_code}\n' http://example.com/
sudo nft list table inet grouproxy
```

控制台应确认：

1. `zhangle` 登录后进入概览，五个节点均能看到心跳。
2. 节点状态为 online/in_sync，monitor 和 sing-box 版本正确。
3. 连接摘要显示节点累计流量；空闲时活动连接为 `0` 是正常状态。
4. 黑名单只下发到所勾选节点，类型为 IP、CIDR 或域名。
5. 角色页中 `zhangle` 是不可变更的 root。

### 10. 升级和故障恢复

升级代码或二进制后，在持有旧进程的机器上重启对应服务：

```bash
# 北京
sudo systemctl restart grouproxy-backend.service
sudo systemctl restart grouproxy-dashboard.service
sudo systemctl restart grouproxy-monitor.service

# 外地节点
sudo systemctl restart grouproxy-monitor.service
```

Monitor 应在新 Bundle 失败时保持 last-good 配置。若节点显示 `rollback_failed` 或 `service_status=unhealthy`，先查看：

```bash
journalctl -u grouproxy-monitor.service -n 200 --no-pager
ls -la /opt/grouproxy/var
sudo nft list table inet grouproxy
```

不要删除 `last-good*`、版本目录或 spool 作为第一步；这些文件是诊断和恢复所需的证据。

## 员工接入

`GROUPROXY_ENVIRONMENT` 决定控制面返回哪一套预生成资产：值精确为 `test` 时使用 `test-proxy.1oa.com.cn`，其他值都使用生产域名 `proxy.1oa.com.cn`。

| 平台 | 接口/资产 | 行为 |
| --- | --- | --- |
| Linux | `/api/v1/access/linux-setup.sh` | 当前用户开关，支持 GNOME/KDE 和 shell/environment 文件 |
| Windows | `/api/v1/access/windows-setup.ps1` | 当前用户 Windows 代理开关 |
| macOS | 接入页中的 `.shortcut` | 下载环境对应的 macOS Shortcut |
| 通用 PAC | `/api/v1/access/proxy.pac` | 返回 HTTP CONNECT `:1080` 的 PAC |
| 配置摘要 | `/api/v1/access/config` | 返回环境、域名、协议和端口，不返回凭据 |

Linux 和 Windows 脚本都是无参数 toggle：运行一次开启，再运行一次关闭。不要把它们当成可任意传参的安装器，也不要修改仓库脚本来注入账号、密码或证书。Backend 必须和 `deploy/` 保持兄弟目录关系，才能正确读取这些资产。

## Docker Compose

`docker-compose.yaml` 只启动 Backend 和 Dashboard，MongoDB 是外部已有服务，不会由 Compose 初始化：

```bash
cp .env.docker.example .env
# 编辑 .env，替换所有 placeholder secret，并填写 MongoDB 地址
docker compose up --build -d
```

默认端口是 Dashboard `80`、Backend `8000`。Backend 使用宿主网络，以便连接测试机上只绑定 `127.0.0.1:27017` 的已有 MongoDB，并保持现有 monitor 的 `http://127.0.0.1:8000` 回连地址；Frontend 通过 `host.docker.internal:8000` 执行服务端 `/api` rewrite。生产环境不应把 `8000` 暴露给公网，节点只需能访问受防火墙保护的 Backend 地址。

要完整替换当前测试环境的原生 dashboard/backend，先停止原生服务，再让 Compose 读取现有测试参数文件：

```bash
sudo systemctl disable --now grouproxy-dashboard.service
GROUPROXY_BACKEND_ENV_FILE=testenv/backend.env docker compose up -d --build
```

`testenv/backend.env` 会作为 Backend 的 `env_file` 传入，因此未在 Compose 清单中逐项列出的测试参数也会保留。唯一的容器适配是将 `GROUPROXY_BACKUP_DIRECTORY` 指向命名卷 `grouproxy-backups`。Compose 不包含 monitor、sing-box 或 MongoDB 容器；节点继续使用已有进程和配置。

Compose 不包含 monitor 和 sing-box。节点仍应使用 systemd 和 `deploy/install-node.sh` 的生产安装方式。Dashboard 镜像在构建期通过 `GROUPROXY_BACKEND_API_URL` 配置 rewrite 目标。

## 配置参考

### Backend 关键环境变量

完整模板见 [`deploy/backend.env.example`](deploy/backend.env.example) 和 [`backend/.env.example`](backend/.env.example)。

| 变量 | 默认/要求 | 说明 |
| --- | --- | --- |
| `GROUPROXY_MONGODB_URL` | 必填 | MongoDB 连接串，生产应带认证和 `authSource` |
| `GROUPROXY_MONGODB_DATABASE` | `grouproxy` | 控制面数据库 |
| `GROUPROXY_BUNDLE_HMAC_SECRET` | 至少 32 字节 | Backend 与所有 monitor 共享 |
| `GROUPROXY_ADMIN_USERNAME` | `zhangle` | 初始操作员 IT code |
| `GROUPROXY_ADMIN_PASSWORD` | 至少 12 字符 | 初始/开发管理员密码 |
| `GROUPROXY_MANAGEMENT_TOKEN` | 至少 32 字节 | 脚本和节点注册用管理 Bearer |
| `GROUPROXY_ENVIRONMENT` | `development` | `test` 选择测试员工资产，其余选择生产资产 |
| `GROUPROXY_HOST` / `PORT` | `0.0.0.0:8000` | Backend 监听地址 |
| `GROUPROXY_ALLOW_INSECURE_AGENT_HTTP` | `false` | Backend URL 为 HTTP 时必须显式为 `true` |
| `GROUPROXY_CORS_ALLOWED_ORIGINS` | 开发有 localhost | 正式同源 Dashboard 建议留空 |
| `GROUPROXY_SEED_DEFAULT_SITES` | `true` | 是否初始化默认五站点 |
| `GROUPROXY_CONNECTION_HISTORY_RETENTION_DAYS` | `90` | 连接摘要历史保留天数，范围 7–3650 |
| `GROUPROXY_AUTH_SESSION_TTL_MINUTES` | `43200` | 管理会话默认 30 天，上限也是 30 天 |
| `GROUPROXY_GQUAN_DELIVERY_MODE` | `app` | 正式使用 GQuan APP API |
| `GROUPROXY_GQUAN_APP_TOKEN` | app 模式需要 | One Login APP Bearer token |
| `GROUPROXY_BACKUP_DIRECTORY` | 空 | 备份目录；生产建议显式设置 |
| `GROUPROXY_BACKUP_ENCRYPTION_KEY` | 生产应设置 | 加密备份密钥，必须长期稳定保存 |
| `GROUPROXY_BACKUP_AUTO_ENABLED` | `false` | 自动维护和保留开关 |
| `GROUPROXY_SUBSCRIPTION_INLINE_MAX_BYTES` | `128000` | 小订阅内嵌 Bundle 的阈值 |
| `GROUPROXY_SUBSCRIPTION_MAX_BODY_BYTES` | `2000000` | HTTP 订阅最大响应体 |

### Monitor YAML 关键字段

模板见 [`deploy/monitor.yaml.example`](deploy/monitor.yaml.example)。

| 字段 | 说明 |
| --- | --- |
| `backend_url` | Backend 地址；HTTP 必须同时打开 `allow_insecure_http` |
| `node_id` | 必须等于控制面注册的 `agent_id` |
| `token_file` | 节点 token 文件，建议权限 `0600` |
| `state_dir` | last-good、运行配置、日志、版本和遥测 spool |
| `singbox_bin` / `singbox_config` | sing-box 二进制和实际配置路径 |
| `listen_port` | 必须为 `1080`；备用端口仅允许显式同机测试覆盖 |
| `firewall_mode` | 生产用 `apply`，开发可用 `dry-run` |
| `hmac_secret` | 必须与 Backend 的 Bundle HMAC secret 相同 |
| `run_singbox` | 正式应为 `true`，由 monitor 管理子进程 |
| `clash_api_listen` | 必须是 loopback `host:port` |
| `poll_interval_seconds` | Desired Bundle 轮询周期，默认 15 秒 |
| `heartbeat_interval_seconds` | 心跳周期，默认 15 秒 |
| `health_window_seconds` | 新配置保持健康的观察窗口，默认 30 秒 |

## 常见问题

### `readyz` 失败，`healthz` 正常

这通常表示 Backend 进程活着但 MongoDB 未连接或 ping 超时。检查连接串、认证、`authSource=admin`、MongoDB 绑定地址和北京本机防火墙。

### 节点 offline

检查目标节点到北京 `8000` 的网络 ACL、`backend_url`、`node.token`、Backend 日志和 monitor 日志。确认 token 对应注册时的 `agent_id`，且 Backend 与 monitor 的 HMAC secret 只影响 Bundle 验证，不要混用两种 token。

### 节点 `in_sync` 但代理不可用

检查 `sing-box` 进程、`1080` 监听、loopback Clash API、nftables 表和 `last-good`。空黑名单应允许流量；如果策略为空仍被拒绝，优先检查是否残留旧 nftables 表或错误启用了其他产品的 `sing-box.service`。

### 发布失败或自动回滚

在控制台查看 release detail、ACK 和 task；节点上检查 `sing-box check`、上游订阅解析、源域名黑名单 DNS 解析、磁盘权限和健康窗口日志。正常行为是保留 last-good，而不是强行覆盖失败候选。

### 端口冲突

- `80`：只在北京运行 Dashboard。
- `8000`：Backend 使用，限制为节点可达。
- `1080`：所有真实节点的公开代理端口，不能随意改动。
- Clash API：只能改成其他 loopback 端口，不能改为公网监听。
- `18081`：只属于同机测试 harness，不可作为正式第二节点协议。

## 限制与安全注意事项

- 当前正式链路全是 HTTP；在不可信网络上部署前必须先完成 TLS/证书设计，不能把 `allow_insecure_http` 当作安全方案。
- 代理数据面没有应用层账号密码，安全边界是办公网 DNS、节点网络 ACL、nftables 黑名单和节点本身的主机权限。
- 管理 token、节点 token、Bundle HMAC secret、管理员密码、GQuan APP token 和备份加密密钥都不能提交到 Git、日志或浏览器配置。
- 订阅包含上游服务器和凭据等敏感内容。控制面只向被选中的节点提供对应版本；管理 UI 和代理配置快照会尽量脱敏。
- Backend 的内存限流适合单实例部署；若未来运行多实例，需要改为共享限流存储并重新评估会话、任务和发布一致性。
- 仓库没有 MongoDB Compose 服务、监控告警外部集成或自动 CI/CD 发布流程，这些属于部署方职责。

## 相关文档

- [`deploy/README.md`](deploy/README.md)：正式五地部署的逐步命令和网络约定。
- [`backend/README.md`](backend/README.md)：Backend API、认证、订阅和安全实现说明。
- [`monitor/README.md`](monitor/README.md)：monitor 构建、Bundle 校验、回滚和本地路由数据说明。
- [`singbox/README.md`](singbox/README.md)：固定 sing-box 版本、平台和 SHA-256。
- [`deploy/backend.env.example`](deploy/backend.env.example)：生产 Backend 环境变量模板。
- [`deploy/monitor.yaml.example`](deploy/monitor.yaml.example)：节点 monitor 配置模板。
