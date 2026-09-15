# Grouproxy 五地部署（北京控制面）

面向正式环境：在 **北京、天津、昆山、深圳、杭州** 各放一台节点，**只把 dashboard 和控制面放在北京**。员工流量只走本节点 `:1080` 的 HTTP CONNECT，不经过控制面。当前不启用 HTTPS。

| 地点 | 跑什么 | 对外端口 |
|---|---|---|
| 北京 | MongoDB + 控制面 backend `:8000` + dashboard `:80` + monitor + sing-box | `:80` 控制台，`:1080` 代理，`:8000` 给各地 monitor（不要对公网开放） |
| 天津 / 昆山 / 深圳 / 杭州 | 只跑 monitor + 其拉起的 sing-box | `:1080` 代理 |

不要在北京以外的机器上安装 dashboard，也不要启用仓库里的 `deploy/sing-box.service`。monitor 自己拉起 `/opt/grouproxy/bin/sing-box`。如果那台机器上已经有别的产品的 `sing-box.service`（例如 QQ 音乐），**禁止覆盖** `/etc/systemd/system/sing-box.service`。

正式员工代理域名是 `proxy.1oa.com.cn`（由 `GROUPROXY_ENVIRONMENT=production` 选定）。测试环境才用 `test-proxy.1oa.com.cn`。

## 网络与 DNS

员工脚本连的是 `http://proxy.1oa.com.cn:1080`。DNS 不区分端口，所以要拆开：

1. **各地办公网 DNS**（或 hosts）：把 `proxy.1oa.com.cn` 解析到 **本节点 IP**，员工代理走本地 `:1080`。
2. **控制台**：浏览器打开 `http://proxy.1oa.com.cn/`（`:80`）必须打到 **北京**。北京以外节点不要在 `:80` 上跑 Grouproxy dashboard。外地管理员打开控制台时，使用指向北京的解析，或直接用北京 IP。
3. **各地 monitor → 北京 `:8000`**：四地到北京 TCP `8000` 必须通；MongoDB `:27017` 只绑北京本机。
4. Clash API 只能绑 loopback（默认 `127.0.0.1:9090`）。本机已被占用时改成 `127.0.0.1:19091` 这类空闲端口，不要监听到 `0.0.0.0`。

空黑名单必须 fail-open（nft 对 `:1080` 放行）。唯一策略是按节点下发的黑名单：IP / CIDR / 域名，来源或目标。

根管理员固定为 **zhangle**。其他人可在「角色」里设为管理员，权限与根管理员相同。不能再产生第二个 root，也不能停用或降级 zhangle。

## 目录约定

北京与各地节点都使用同一套路径：

```text
/opt/grouproxy/src          # git clone（backend、deploy、monitor、singbox、frontend）
/opt/grouproxy/venv         # 仅北京：Python 3.12 虚拟环境
/opt/grouproxy/dashboard    # 仅北京：Next standalone
/opt/grouproxy/bin          # grouproxy-monitor、sing-box
/opt/grouproxy/etc          # backend.env（仅北京）、monitor.yaml、node.token
/opt/grouproxy/var          # monitor 状态、last-good、sing-box.json
```

`backend/app/services/access.py` 会从源码树旁的 `deploy/` 读取员工脚本，因此北京必须保留完整仓库布局（`backend/` 与 `deploy/` 是兄弟目录），不要只拷 `backend` 一个文件夹。

建议的站点显示名与 `agent_id`（启动时若 `GROUPROXY_SEED_DEFAULT_SITES=true` 会种子 5 个站点，再在控制台改名）：

| 种子 slug | 显示名 | agent_id |
|---|---|---|
| north | 北京 | beijing |
| east | 天津 | tianjin |
| central | 昆山 | kunshan |
| south | 深圳 | shenzhen |
| west | 杭州 | hangzhou |

`agent_id` 写入后不可改，只可以改显示名。

---

## 1. 北京：控制面 + dashboard + 本节点

### 1.1 系统依赖

- Python 3.12、Node.js（只为构建 dashboard）、nftables、MongoDB（本机）
- 仓库内已带 Linux amd64 的 `singbox/sing-box` 和 `monitor/dist/grouproxy-monitor-linux-amd64`

```bash
sudo groupadd --system grouproxy 2>/dev/null || true
sudo useradd --system --gid grouproxy --home-dir /opt/grouproxy --shell /usr/sbin/nologin grouproxy 2>/dev/null || true
sudo install -d -m 0755 /opt/grouproxy/bin /opt/grouproxy/dashboard
sudo install -d -o grouproxy -g grouproxy -m 0750 /opt/grouproxy/etc /opt/grouproxy/var
sudo git clone git@github.com:zl875136491/grouproxy.git /opt/grouproxy/src
```

### 1.2 MongoDB

使用已有实例即可，库名建议 `grouproxy`。连接串必须带 `authSource=admin`。控制面启动时会做策略迁移，不要对这个库跑别的应用。

### 1.3 控制面环境变量

复制 `deploy/backend.env.example` 为 `/opt/grouproxy/etc/backend.env`，填入密钥后：

```bash
sudo chmod 0640 /opt/grouproxy/etc/backend.env
sudo chown grouproxy:grouproxy /opt/grouproxy/etc/backend.env
```

必填：

- `GROUPROXY_BUNDLE_HMAC_SECRET`：≥32 字节，**所有节点 `monitor.yaml` 的 `hmac_secret` 必须相同**
- `GROUPROXY_ADMIN_PASSWORD`：≥12 字符；测试/开发环境启动会把 zhangle（以及配置的 `GROUPROXY_ADMIN_USERNAME`）同步成这个口令。生产环境 `GROUPROXY_ENVIRONMENT=production` **不会**在每次启动时覆盖已有口令
- `GROUPROXY_MANAGEMENT_TOKEN`：≥32 字符，给脚本用的管理 Bearer
- `GROUPROXY_MONGODB_URL` / `GROUPROXY_MONGODB_DATABASE`
- `GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true`（当前全站 HTTP）
- `GROUPROXY_HOST=0.0.0.0`（否则外地 monitor 连不上）
- `GROUPROXY_CORS_ALLOWED_ORIGINS=` 留空：dashboard 同源反代时关闭 CORS/CSRF Origin 限制，便于本机用管理 token 注册节点
- 正式登录若走光圈：配置 `GROUPROXY_GQUAN_APP_TOKEN`，不要写进 Git

### 1.4 安装并启动 backend

```bash
sudo python3.12 -m venv /opt/grouproxy/venv
sudo /opt/grouproxy/venv/bin/pip install -e /opt/grouproxy/src/backend
sudo install -m 0644 /opt/grouproxy/src/deploy/grouproxy-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grouproxy-backend.service
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8000/healthz   # 应报告 version 0.6.0
```

确认 `ss -ltnp | grep 8000` 监听在 `0.0.0.0:8000`。用防火墙只允许五地节点 IP 访问该端口。

### 1.5 构建并启动 dashboard（只在北京）

在北京构建，`GROUPROXY_BACKEND_API_URL` 是 **构建期** 写入的 rewrite 目标，必须指向本机 backend：

```bash
cd /opt/grouproxy/src/frontend
sudo -H npm ci
sudo -H env GROUPROXY_BACKEND_API_URL=http://127.0.0.1:8000 npm run build
sudo rm -rf /opt/grouproxy/dashboard
sudo mkdir -p /opt/grouproxy/dashboard
sudo cp -a /opt/grouproxy/src/frontend/.next/standalone/. /opt/grouproxy/dashboard/
sudo chmod -R a+rX /opt/grouproxy/dashboard
sudo install -m 0644 /opt/grouproxy/src/deploy/grouproxy-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grouproxy-dashboard.service
curl -fsS http://127.0.0.1/healthz   # 应转到 backend 0.6.0
```

北京 `:80` 若仍被 nginx 占用，先停掉 Grouproxy 不再使用的旧入口，或把 nginx 迁走。不要在外地节点做这一步。

登录：`http://proxy.1oa.com.cn/`，IT code `zhangle`，口令为 `GROUPROXY_ADMIN_PASSWORD`（首次创建时）。在「角色」里把其他人设为管理员。

### 1.6 改站点名并注册北京节点

种子站点后，在控制台「节点」页改显示名，或：

```bash
TOKEN=$(sudo grep GROUPROXY_MANAGEMENT_TOKEN /opt/grouproxy/etc/backend.env | cut -d= -f2-)
API=http://127.0.0.1:8000
AUTH="Authorization: Bearer ${TOKEN}"

curl -fsS -H "$AUTH" "$API/api/v1/sites" | jq '.[] | {id,slug,name}'
# PATCH /api/v1/sites/{id}  body: {"name":"北京"}  其余四地同理
```

注册节点（只在创建响应里出现一次 `agent_token`，立刻写入 token 文件）：

```bash
SITE_ID=<北京站点 id>
curl -fsS -X POST "$API/api/v1/nodes" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"site_id\":\"$SITE_ID\",\"name\":\"beijing\",\"agent_id\":\"beijing\",\"advertise_ip\":\"<北京节点 IP>\"}" \
  | tee /tmp/node-beijing.json
jq -r .agent_token /tmp/node-beijing.json | sudo -u grouproxy tee /opt/grouproxy/etc/node.token >/dev/null
sudo chmod 0600 /opt/grouproxy/etc/node.token
shred -u /tmp/node-beijing.json
```

### 1.7 北京本节点 monitor

```bash
sudo install -m 0755 /opt/grouproxy/src/monitor/dist/grouproxy-monitor-linux-amd64 /opt/grouproxy/bin/grouproxy-monitor
sudo install -m 0755 /opt/grouproxy/src/singbox/sing-box /opt/grouproxy/bin/sing-box
sudo cp /opt/grouproxy/src/deploy/monitor.yaml.example /opt/grouproxy/etc/monitor.yaml
# 编辑：node_id=beijing，backend_url=http://127.0.0.1:8000，hmac_secret 与控制面相同
# clash_api_listen 避开本机已占用端口；firewall_mode: apply
sudo chown grouproxy:grouproxy /opt/grouproxy/etc/monitor.yaml /opt/grouproxy/etc/node.token
sudo chmod 0640 /opt/grouproxy/etc/monitor.yaml
sudo install -m 0644 /opt/grouproxy/src/deploy/grouproxy-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo -u grouproxy /opt/grouproxy/bin/grouproxy-monitor -config /opt/grouproxy/etc/monitor.yaml -validate
sudo systemctl enable --now grouproxy-monitor.service
```

**不要** `systemctl enable sing-box.service`。

---

## 2. 天津 / 昆山 / 深圳 / 杭州：只装节点

在北京控制面为每个地点各注册一个节点，拿到 `agent_token` 后拷到该机 `/opt/grouproxy/etc/node.token`。

从北京仓库目录对目标机安装二进制（**不会**安装 dashboard，也 **不会** 写入 `sing-box.service`）：

```bash
NUC_SSH_USER=root ./deploy/install-node.sh <节点 IP>
```

然后在目标机写入 `monitor.yaml`（见 `deploy/monitor.yaml.example`）：

- `backend_url: http://<北京 IP>:8000`
- `node_id` 与注册时的 `agent_id` 完全一致
- `allow_insecure_http: true`
- `firewall_mode: apply`
- `hmac_secret` 与北京控制面相同
- `clash_api_listen` 若 `127.0.0.1:9090` 已被占用，改成空闲 loopback 端口

```bash
sudo -u grouproxy /opt/grouproxy/bin/grouproxy-monitor -config /opt/grouproxy/etc/monitor.yaml -validate
sudo systemctl enable --now grouproxy-monitor.service
```

手工安装时只拷 `grouproxy-monitor`、`sing-box` 二进制和 `grouproxy-monitor.service`。不要 scp `deploy/sing-box.service`。

---

## 3. 验收

在北京：

```bash
curl -fsS http://127.0.0.1/healthz
curl -fsS http://127.0.0.1:8000/readyz
ss -ltnp | grep -E ':80 |:8000 |:1080 '
```

各地：

```bash
systemctl is-active grouproxy-monitor.service
# 必须 inactive 或不存在：
systemctl is-active grouproxy-dashboard.service || true
ss -ltnp | grep 1080
curl -sS --max-time 8 -x http://127.0.0.1:1080 -o /dev/null -w '%{http_code}\n' http://example.com/
sudo nft list table inet grouproxy
```

控制台：

1. zhangle 登录后应进入「概览」，而不是员工接入页。
2. 五节点均为 online / in_sync，版本 0.6.0。
3. 「连接摘要」能看到各节点累计流量；空闲时活跃连接为 0 正常。
4. 「黑名单」按节点勾选，类型为 IP / CIDR / 域名。规则只下发到勾选节点。
5. 「角色」里 zhangle 为根管理员且不可变更。

升级时在每台相关机器上重启仍持有旧二进制的进程：

```bash
# 北京
sudo systemctl restart grouproxy-backend.service
sudo systemctl restart grouproxy-dashboard.service
sudo systemctl restart grouproxy-monitor.service
# 外地节点
sudo systemctl restart grouproxy-monitor.service
```

## 4. 员工接入

生产环境资产由控制面按 `GROUPROXY_ENVIRONMENT=production` 选择：

- Linux：`GET /api/v1/access/linux-setup.sh`（无参数开关，目标 `proxy.1oa.com.cn:1080`）
- Windows：`GET /api/v1/access/windows-setup.ps1`
- macOS：接入页上的 Shortcut

不要改这些脚本当模板；文件必须保持在仓库 `deploy/` 里与 backend 一起部署。

## 5. 本地双节点测试（不要当生产拓扑）

```bash
GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@127.0.0.1:27017/?authSource=admin' \
  GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
```

`testenv/` 被 Git 忽略。同源双进程仿真里第二节点会用 `:18081`；真实第二台机器仍用 `:1080`。当前测试机约定是 111 跑 dashboard、110 只跑 monitor。
