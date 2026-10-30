"""stun — 自建单容器 DNAT/STUN 管理工具（摆脱 Lucky，管理多 macvlan 服务映射）。

模块说明：
- config : 环境变量配置
- db     : SQLite 数据层（映射表 + STUN 服务器表）
- ikuai_client : 爱快 v4.0 REST API 客户端（Bearer 令牌），含离线 Mock
- stun_probe   : STUN 探测 / NAT 类型检测 / 服务器池扫描
- poller : 后台轮询线程（端口变化只改 wan_port、失败挂起与恢复、NAT 复检）
- main   : FastAPI 后端 + WebUI
"""

__version__ = "1.0.0"
