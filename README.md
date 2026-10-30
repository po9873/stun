# stun — 自建 DNAT / STUN 管理器

摆脱 Lucky、自带 WebUI、自动轮询 STUN 服务器、自动创建/更新/删除爱快 DNAT 规则的单容器工具，
用于管理多个 macvlan 下的 tr / qb / Emby / 任意内网服务的端口映射。**只改公网端口，不动内网端口。**

## ★ 两个端口必须分清（无公网 / CGNAT 场景）

用 STUN 本身就说明**没有公网 IP**——STUN 探到的 `119.126.171.249` 是运营商 CGNAT 的出口 IP，
不是你爱快的 WAN 地址。因此存在两个**含义完全不同**的端口：

```
外部用户 ──▶ 119.126.171.249:5055   ← 「公网访问端口」= STUN 实测（运营商分配，不可控）
                  │  CGNAT 做端口转换：5055 ⇄ 51999
            爱快 WAN:51999          ← 「爱快侧端口」= DNAT 规则的 wan_port
                  │  爱快 DNAT（端口保留）
            192.168.6.13:51999
```

- **爱快侧端口 = 内网端口**（写进 DNAT 的 `wan_port`）。
  因为 CGNAT 是「内网端口 ⇄ 公网端口」的双向映射：入站时它把 `5055` 还原成 `51999`，
  到达爱快的报文目的端口就是 `51999`。
- **公网访问端口 = STUN 实测值**（如 `5055`），只用来告诉别人「连这个地址」，
  **不能**写进爱快 DNAT——写进去规则就匹配不上了。

### 为什么是这样（可自行复验）

判别方法只有一条：**看 STUN 实测端口和内网端口是否相等**。

- 实测时本环境给出 `内网 80 → 公网 3040`、`内网 51999 → 公网 5055`，两者都不相等，
  说明改端口的是外层 CGNAT（爱快自己端口保留）。既然是 CGNAT 在做 `内网端口 ⇄ 公网端口`
  的映射，入站必然还原成内网端口，爱快规则就应该写内网端口。
- 更直接的证据：**你爱快上原本那 11 条真正在用的映射（QB/TR/Emby），
  全部是「外网端口 = 内网端口」**（5533→5533、46065→46065、51413→51413 ……）。
  这些是做种/刷流用的端口，入站必须通，配置一律是端口保留。
- 若实测端口 == 内网端口（说明整条链路没人改端口），两种模式结果完全一致，怎么写都对。

因此默认模式是 **端口保留（preserve）**：
- 表格「外网端口(爱快)」= 内网端口 = 写进 DNAT 的 `wan_port`
- 表格「公网访问地址」= STUN 实测的 `IP:端口`，仅用于对外发布

设置页保留了 **STUN 实测值（stun）** 模式开关，仅供「改端口的是爱快自己、
CGNAT 只做 1:1 透传」这类罕见拓扑使用。切之前先看实测端口等不等于内网端口。

## 它解决什么

- **一键接管路由上已有的 DNAT 规则**：扫描 → 勾选 → 接管，不改动路由任何设置
- 自动维护 STUN 服务器池（来自 always-online-stun 的实测可用清单），按延迟自动排序，不用手填
- **WebUI 里直接填爱快地址 / 个人 API 令牌 / WAN 口名**，不写进 compose
- 添加映射时**自动在爱快创建对应 DNAT 规则**，拿到规则 ID 入库
- **每 5 分钟与爱快对账**：读回路由上真实生效的 `wan_port`，不一致直接推回去，
  并把「本地 vs 路由」的差异在表格里标红显示（不靠本地记录自欺欺人）
- 后台每分钟轮询：端口变化时**只改 `wan_port`**，`lan_addr` / `lan_port` 不变
- 支持任意 macvlan 实例（服务无关，按「名称 / 内网 IP / 端口 / 协议」映射）
- 已关闭 UPnP 也不影响（走爱快 REST API 直建静态 DNAT）
- NAT 类型检测；NAT4（对称型）暂停主轮询并红字告警；每小时复检
- 失败策略：单映射连续 5 次失败挂起（删爱快规则、保留内网信息）；NAT 恢复自动重建

## ★ 接管已有规则：公网端口测不准，别拿去用

不想一条条手填？映射管理页的「从爱快导入已有规则」三步搞定：

1. 「扫描爱快上的规则」→ 列出路由全部 DNAT 规则，并标注 **待接管 / 已接管 / 不可接管**
2. 勾选要纳入管理的（或点「一键全部接管」）
3. 接管完成

**原规则 ID、内网地址、外网端口全部沿用，路由上一个字节都不动**（不删不重建）；
爱快上已禁用的规则会被接管为「挂起」，对账不会去碰它。
重复点导入会被自动跳过（按规则 ID 与「内网 IP+端口+协议」双重去重）。

以下两类规则会被标为 **不可接管** 并说明原因，避免接过来反而推错端口：
端口范围型（如 `1000-2000`）、取不到内网地址的规则。

但接管后，「公网访问地址」这一列对大多数映射会显示 **不可测**，这是有原因的：

```
容器自己  192.168.6.104:80     ──STUN──▶ 119.126.171.249:3366   ✔ 能测、有意义
别的机器  192.168.6.13:46065   ──?─────▶ ???                    ✘ 测不出来
```

容器只能绑定自己的 IP。让容器去"冒充" `192.168.6.13` 发 STUN 请求，
CGNAT 看到的仍然是容器那条映射（`119.126.171.249` 后面是容器，不是 QB 那台机），
测出来的端口号属于容器，**与 QB 实际持有的公网端口毫无关系**。
CGNAT 给不同内网 IP 分配的是各自独立的公网端口。

所以：

- 目标机 ≠ 容器 IP 的映射 → 标记 `probe=0`，**不轮询探测**（省掉每轮 N 次无效 STUN 握手），
  公网地址显示「不可测」
- 目标机 = 容器 IP 的映射 → 正常探测，公网地址可用
- 无论哪种，**stun 模式下都不会拿跨 IP 的结果去改路由**，
  否则会把 QB/TR 的规则改成一堆无效端口，直接失联

想要某台机器的真实公网端口，只能由那台机器自己发出连接（它自己跑 STUN 客户端）。

### 附：CGNAT 端口是会漂移的

实测同一内网端口 46065：

| 探测方式 | 结果 |
|---|---|
| 连续 3 次（无间隔，同一服务器） | `4825 / 4825 / 4825` 稳定 |
| 间隔 75 秒 | `5010 → 3628 → 2703` 每次都变 |

说明这个 CGNAT 的映射条目闲置后会超时回收，再连接就换新端口。
想让某个公网端口保持稳定，需要持续心跳（比如缩短轮询间隔）来保活；
QB/TR 做种时本身就在持续发连接，它们自己的条目是活的。

## 技术栈

FastAPI + 原生 HTML/JS + SQLite + **自实现的 RFC 5389 STUN 客户端（TCP+UDP）** +
自实现的爱快 v4.0 REST 客户端（Bearer 令牌）。

## ★ 为什么走 TCP STUN（重要）

实测本环境：**UDP 3478 完全不通**（运营商/路由封锁），而 **TCP 3478 正常**。
原依赖 `pystun3` 只支持 UDP，探测必然全部超时 → WebUI 显示「NAT 类型: blocked」。

现已移除 pystun3，改为自带的纯标准库实现：

- 传输顺序：**先 TCP，失败再回退 UDP**（RFC 5389 §7.2.1，TCP 上裸报文无长度前缀）
- 每个服务器的实际可用传输方式会显示在 WebUI 的「传输」列（TCP / UDP / 未测）
- NAT 检测同样优先 TCP：先常规 BINDING，再用「同一本地端口访问两个服务器」
  比较映射端口差异来判定对称型（对称 → NAT4）

实测结果（容器 192.168.6.104 内）：

```
stun.vomessen.de  local 192.168.6.104:38072 -> mapped 119.126.171.249:3745
stun.nextcloud.com local 192.168.6.104:45720 -> mapped 119.126.171.249:3754
```

## 已真机验证的爱快接口契约（路由 192.168.6.254 / 固件 4.0.313）

```
Base   = https://<host>/api/v4.0
Auth   = Authorization: Bearer <个人API令牌>
TLS    = 自签证书，默认跳过校验
列表   GET  /network/dnat/rules        -> {code:0, results:{data:[...]}}
创建   POST /network/dnat/rules        -> {code:0, rowid:<规则ID>}
更新   PUT  /network/dnat/rules/{id}   （需传完整字段；本工具先取回再合并，只改 wan_port）
删除   DELETE /network/dnat/rules/{id}
启停   PATCH /network/dnat/rules/{id}  {"enabled":"yes"/"no"}
```

创建 body：`tagname / lan_addr / lan_port / wan_port / protocol / interface / enabled / comment / src_addr`。
端口与 `enabled` 均为字符串；`protocol` 支持 `tcp` / `udp` / `tcp+udp` / `any`。
实测 WAN 接口名为 `adsl1`，协议合并格式为 `tcp+udp`（正好覆盖 qb 的 tcp+udp 同端口）。

> 注意：`/api/v4.0/network/dnat/rules` 加 `?limit=` 分页参数会 500，列表用不带参数的 GET。

## 部署

### 容器名 / 栈名 / 镜像名统一为 `stun`

| 位置 | 名称 |
|---|---|
| GitHub 仓库 | `stun` |
| Docker Hub 镜像 | `po9873/stun:latest` |
| Portainer 栈 | `stun` |
| 容器名 | `stun` |

> 早期版本叫过 `stum`，已全部改回 `stun`。老栈/老镜像/老卷直接删掉重建即可，
> 数据库可用「导出 → 导入 /data/stun.db」的方式迁移（配置、令牌都跟着走）。

### Portainer CE 部署步骤

1. 把整个 `stun/` 目录压缩成 zip。
2. Portainer → **Stacks → Add stack** → **Upload** 上传 zip。
3. **必改**：`docker-compose.yml` 里 macvlan 网络名 `bridge-host` → 改成你宿主机上
   已存在的 macvlan 网络名（在 Portainer「Networks」里看）。
   ⚠ 不要新建 macvlan 网络：新建会与已有网络抢 `192.168.6.254` 网关，
   报 `failed to allocate gateway ... Address already in use`。
4. 点 **Deploy**。
5. 打开 **http://192.168.6.104** → 「设置」页填爱快地址 / 令牌 / WAN 口名 → 点「保存并试连」。

容器固定 IP `192.168.6.104`、固定 MAC `02:42:C0:A8:06:68`。

### 已知部署坑（都已处理）

| 现象 | 原因 | 处理 |
|---|---|---|
| `yaml: control characters are not allowed` | Portainer 的 `method=file` 不解压 zip，把二进制当 YAML | 改用 `create/standalone/string` 传 compose 文本 |
| `undefined volume stun_data` | compose 引用了卷但顶层没声明 | 顶层补 `volumes: stun_data:` |
| `Get https://registry-1.docker.io/v2/` | 容器内没镜像，尝试联网拉取 | 先用 Docker build API 在宿主机构建 `stun:local`，compose 用 `image:` 不用 `build:` |
| `failed to allocate gateway` | 新建 macvlan 与已存在的抢网关 | 复用宿主机已有的 macvlan 网络（`external: true`） |
| WebUI 显示 `blocked` | UDP 3478 被封 | 改用 TCP STUN |
| `Cannot locate specified Dockerfile` | 构建上下文 tar 里源码套了一层目录 | tar 以项目目录为根打包（`tar -C 项目 -czf x.tar.gz .`） |
| `COPY failed: file not found ... requirements.txt` | 同上，路径对不上 | 同上 |

> macvlan 特性：容器与宿主机（192.168.6.38）之间**默认不互通**，但容器内可正常访问
> iKuai（192.168.6.254）与公网 STUN 服务器；从局域网其他电脑访问 http://192.168.6.104 正常。

## 发布到 GitHub + Docker Hub

仓库里已带好 GitHub Actions（`.github/workflows/docker-publish.yml`）：
推 `main`/`master` 或打 `v*` 标签时自动构建 **amd64 + arm64** 多架构镜像并推到 Docker Hub。

只需在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 里加两个 secret：

| Secret | 内容 |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub 用户名（本例 `po9873`） |
| `DOCKERHUB_TOKEN` | Docker Hub 的 **Access Token**（不是登录密码，在 Account Settings → Security 里生成） |

首次上线顺序：

```bash
# 1. GitHub 上先建一个名为 stun 的空仓库（不要勾 README）
git remote add origin https://github.com/<你的用户名>/stun.git
git push -u origin main

# 2. 推 v1.0.0 标签，触发多架构构建
git tag v1.0.0 && git push origin v1.0.0
```

镜像推上去后，Portainer 里把栈的 `STUN_IMAGE` 改成 `po9873/stun:latest` 重新部署即可，
compose 里默认就是这个值。

## 配置方式

### 方式一：WebUI 设置页（推荐）

打开 WebUI → 「设置」页，填写：

| 项 | 示例 | 说明 |
|---|---|---|
| 爱快管理地址 | `https://192.168.6.254` | 只填到路由地址，`/api/v4.0` 由程序自动拼接 |
| 个人 API 令牌 | `MjU0...` | 留空表示沿用已保存的令牌 |
| WAN 接口名 | `adsl1` | 爱快「网络 → 接口」里外网口的名字 |
| 校验 HTTPS 证书 | 否 | 自签证书需选「否」 |

改完立即生效，无需重启容器。令牌只回显「已设置/未设置」，不会明文返回给前端。

### 方式二：环境变量（无人值守部署时可选）

优先级：**WebUI 设置 > 环境变量 > 内置默认值**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `IKUAI_CLI_BASE_URL` | `https://192.168.6.254` | 爱快管理地址（自动补 `/api/v4.0`） |
| `IKUAI_CLI_TOKEN` | 空 | 个人 API 令牌 |
| `IKUAI_INTERFACE` | `adsl1` | WAN 口名 |
| `IKUAI_VERIFY_SSL` | `false` | 自签证书跳过校验 |
| `WEB_PORT` | `80` | WebUI 端口 |
| `DATA_DIR` | `/data` | SQLite 持久化目录（挂载卷） |
| `POLL_INTERVAL` | `60` | 映射轮询间隔（秒） |
| `STUN_SCAN_INTERVAL` | `3600` | STUN 服务器扫描间隔（秒） |
| `NAT_CHECK_INTERVAL` | `3600` | NAT 复检间隔（秒） |
| `RECONCILE_INTERVAL` | `300` | 与爱快对账间隔（秒） |
| `WAN_PORT_MODE` | `preserve` | `preserve`=外网端口跟随内网端口（默认）；`stun`=跟随 STUN 实测值 |
| `STUN_TIMEOUT` | `4` | 单次探测超时（秒） |
| `STUN_POOL_URL` | always-online-stun | STUN 清单地址，粘 GitHub 网页地址也会自动纠正成直链 |
| `IKUAI_MOCK` | `false` | 离线 Mock，不连真路由 |

兼容旧名 `IKUAI_URL` / `IKUAI_TOKEN`。

## 运行逻辑

- **添加映射**：建库 → 调爱快创建 DNAT 拿 `rule_id` → 立即 STUN 探测刷新公网端口。
- **对账**：每 5 分钟从爱快读回全部规则的真实 `wan_port`，与本地不一致就推回去。
  这是为了兜住「某次推送失败，之后数值再没变过，常规轮询就不再推」导致的长期不一致。
  推送失败时本地不落新值，下一轮会自然重试，不会形成僵局。
- **轮询**：每分钟遍历 active 映射，用各自内网端口问 STUN；公网端口变了只改 `wan_port`；
  单映射连续失败 5 次 → 挂起（删爱快规则、保留内网信息、灰色显示、可手动恢复）。
- **NAT 复检**：每小时一次；NAT4 → 全部挂起；恢复 → 自动重建规则。
  仅 `nat4` / `blocked` 会阻断，`unknown` / `error` 只告警不挂起（避免误伤）。
- **STUN 池**：每小时扫描延迟/可达性，连续失败 3 次降级；可用节点按延迟排序取最快。
  种子池来自 `app/stun_servers.txt`，WebUI 可手动扫描/添加/删除。

### 关于 qb/tr 这类 TCP+UDP 同端口

探测时会尝试绑定 `(lan_ip, lan_port)`：

- 绑定成功 → **精确探测**（结果最可靠）
- 绑定失败（macvlan 容器内无法绑其他容器的 IP，或端口已被服务占用）
  → 退化为绑定 `0.0.0.0:<同一端口>`，端口号一致、源 IP 为本容器
  → 再失败才用随机源端口

该判断按端口维度逐映射进行，任意服务通用，WebUI 可手动指定公网端口兜底。

## 本地离线测试（不连真路由）

```bash
pip install -r requirements.txt
IKUAI_MOCK=true python -m app.main
# 打开 http://127.0.0.1
```

`IKUAI_MOCK=true` 时所有爱快调用走内存 Mock，可完整验证 WebUI / 轮询 / STUN 扫描流程。

## 安全提示

- 爱快个人 API 令牌等同于账号密码，建议用**非永久、仅端口映射权限**的令牌长期运行。
- 令牌存在 `/data` 卷的 SQLite 里、不进 compose，但 `/data` 卷本身仍需保护好。
