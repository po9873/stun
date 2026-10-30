# stun — 自建 DNAT / STUN 管理器

无公网（CGNAT）环境下，自动管理爱快的端口映射，并把每条映射对外真正可用的
**公网访问地址**测出来、端口漂了自动更新并推送通知。用来替代 Lucky 的端口转发。

## 爱快上就是一条普通端口映射

内网地址、内部端口原样不动，外网端口 = 内部端口，不引入任何额外端口，
流量全程不经过容器（来源 IP 保留）。

## 公网端口怎么测出来的

容器从**目标机那个端口号**往外发包：爱快保留源端口，出去时外部口也是同一个端口号，
于是和目标机自己的流量共用 CGNAT 上同一条映射 —— 问 STUN 服务器拿到的就是外部
真正能连进来的那个端口（实测 `.104:24213 → 公网 2307`，连 2307 直达 `.248:24213`）。

内网机器**什么都不用装**，容器自己就能把全部映射测出来。要守住三条：

1. 按协议挑服务器 —— CGNAT 上 TCP 与 UDP 是两套端口，UDP 测出的端口 TCP 连不进。
2. 每条映射固定同一台服务器 —— 换一台就是新开一条流、拿到一个新公网端口。
3. 保持住 TCP 连接 + keepalive，每 20 秒在同一条连接上问一次 ——
   CGNAT 是每条新连接一个新公网端口，重连就换号。

## 功能

- 添加 / 批量录入 / 从爱快导入已有规则（沿用原规则 ID，不动路由）
- 每 20 秒测一轮公网端口（并行），每 5 分钟与爱快对账，不一致自动推回去
- 公网 IP 或公网端口变化 → Webhook 推送（钉钉 / 企微 / 飞书 / Bark / 自定义）
- STUN 服务器池自动扫描与更新

## 部署

镜像：本地 `stun:local`，或推到 Docker Hub 后改 `docker-compose.yml` 里的 `image:`。

Portainer → Stacks → Add stack → Web editor 粘贴 `docker-compose.yml`。
macvlan 网络复用宿主机已有的 `bridge-host`，容器固定 192.168.6.104。

部署后打开 WebUI →「设置」页填爱快地址 / 个人 API 令牌 / WAN 口名（存进 /data 卷）。

## 发布到 GitHub + Docker Hub

```bash
git tag v1.0.0 && git push origin main --tags
```

Actions 会构建 linux/amd64 + arm64 并推到 Docker Hub。需要在仓库 Settings →
Secrets 里配 `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`。

## 注意

- qBittorrent / Transmission 里的「测试端口」永远是红的：它探的是
  `公网IP:内网端口`，而 CGNAT 下外部能连的是 `公网IP:公网端口`，两者不同。
  外部访问用表格里的**公网访问地址**。
- 爱快令牌建议用非永久、仅端口映射权限的。
