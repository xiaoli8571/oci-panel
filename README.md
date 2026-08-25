# 🛞 CloudDeck · 云驭 — 轻量自托管多云管理面板

> **原 OCI Manage Lite**,现已成长为覆盖 **Oracle Cloud / AWS / IBM Cloud** 的轻量自托管多云面板。
> Python (FastAPI) + 原生前端,单容器部署、无外部数据库依赖、代码全开源可审计。

`self-hosted` `oracle-cloud` `aws` `ibm-cloud` `cloudflare` `vps` `web-ssh` `sftp` `telegram-bot` `fastapi`

---

## ✨ 功能总览

### ☁️ 多云实例管理

| 能力 | 说明 |
|---|---|
| 🖥 实例总览 | 多账户/多区域并行扫描,stale-while-revalidate 缓存;状态、公网 IP(临时/保留)、私网 IP、规格、启动盘一览 |
| ⏻ 电源操作 | 启动 / 软关机 / 强制关机 / 重启(OCI · AWS EC2/Lightsail · IBM VPC) |
| 🔄 换公网 IP | 一键换临时 IP(OCI 自动关开机;AWS/IBM 释放重申);可选联动 Cloudflare A 记录自动更新 |
| 🌐 IPv6 / 保留 IP | OCI IPv6 一键附加;保留公网 IP 新建/删除/绑定/解绑 |
| 🔓 端口开放 | 一键向安全列表追加 TCP 入站规则(22/80/443/自定义,IPv4+IPv6) |
| 💽 卷管理 | 启动盘扩容、VPU 性能层级(Balanced/Better/Ultra) |
| 🔧 规格与属性 | A1.Flex 升降配、实例改名、终止(可选保留启动盘) |
| ⚖️ 配额体检 | A1 核数/内存/E2.Micro 在各 AD 的用量余量、订阅类型识别、一键降配 |

### 🚀 创建实例

- **Oracle Cloud**:AMD 微型 / ARM A1 一键预设,**强开 ARM**(容量不足自动重试,次数间隔可调);镜像/子网/AD 可选;**从现有启动盘开机**
- **AWS**:EC2(区域→AMI→实例类型→子网→安全组→密钥对)与 Lightsail(区域→可用区→Blueprint→Bundle)全生命周期
- **IBM Cloud**:VPC → 子网 → 镜像(公开+私有)→ Profile → SSH Key → 可用区

### 🛟 救援系统(特色)

系统盘损坏 / SSH 进不去 / fstab 改错 / 磁盘占满?一键救援:

```
关停故障实例 → 卸下启动盘 → 数据盘方式挂到同 AD 健康实例
→ Web SSH 登录目标机修复(chroot 改密码 / 修 fstab / 清磁盘)
→ 「完成还原」自动装回启动盘并开机
```

软关机超时自动强停、全程状态校验防呆、会话持久化(中途关面板不丢)、
与守护中心联动(救援中的实例不会被保活拉起)。

### 💻 远程运维

- **Web SSH**:浏览器多标签终端(xterm.js),密码/私钥认证,主机指纹 TOFU 校验,云主机一键导入
- **SFTP 文件管理**:浏览/上传/下载/重命名/删除/新建目录,单文件 ≤50MB
- **SSH 批量命令**:多台并行执行,逐台展示退出码与输出
- **凭据库**:SSH/SFTP 凭据加密保存,免重复输入

### 🛡 守护中心

- **停机保活**:停机实例自动拉起(可按账户开关)
- **月流量守护**:超阈值通知或自动关停(被关停账号当月不保活)
- **事件历史** + Webhook 通知(Telegram / Bark / Server酱)
- **定时任务**:按每日时间(可选星期)自动 开机/关机/重启,30 秒粒度、同刻去重、试执行

### 🌐 DNS 与域名

- **Cloudflare**:域名/记录增删改查、Workers 部署与管理、Git 仓库一键部署 Worker、路由绑定
- **HE.net / DNSHE**:记录管理、DDNS 接口
- **域名 & SSL 到期监控**:RDAP + TLS 握手双检测,≤30/14/7/3/1 天分档推送(Telegram/Webhook)

### 🗄 其他

- **对象存储**:OCI 桶与对象管理(浏览/上传 ≤50MB/下载/删除)
- **📈 流量统计**:`oci_computeagent` 指标,24h/7d/30d 图表
- **🤖 Telegram Bot**:`/status` `/list` `/ip` `/on` `/off` `/reboot` `/reip` `/open` `/quota` `/dom` `/stats` 等指令遥控
- **📜 操作审计**:全部 API 写操作留痕,账户页可查询过滤
- **⬆ 版本检查** / **☀️ 浅深主题**(跟随系统) / **healthz 健康检查**

## 🆕 更新日志

### v0.21.0(品牌升级:CloudDeck)

- 🛞 面板更名为 **CloudDeck(云驭)**,原 OCI Manage Lite;功能不变,数据目录无缝兼容
- 全部品牌触点更新:登录页/顶栏/标题、TG 测试消息、User-Agent、compose 服务名、镜像路径
- 内置版本检查指向新仓库 `xiaoli8571/clouddeck`

### v0.20.x(救援系统 + 多云修复)

- 🛟 v0.20.0 实例救援:故障盘跨实例挂载离线修复,完成自动装回开机
- 🐛 v0.20.1/v0.20.2:IBM 公网 IP 三数据源交叉补全(区域浮动IP表/单网卡详情/主网卡兜底)+ `/api/ibm/net-debug` 诊断接口;AWS Lightsail 创建必填可用区自动补全(AZ 下拉选择)

### 历史

<details>
<summary>展开完整更新历史</summary>

- v0.19.x:IBM Cloud 补齐创建/终止;IBM VPC API version 参数修复
- v0.18.0:AWS EC2 创建补齐,AWS 接入完整(EC2+Lightsail 全生命周期)
- v0.17.0:接入 IBM Cloud VPC(列表/电源/换IP);DNSHE 子域名列表修复
- v0.16.0:从启动盘开机(R探长同款);TG /reip 增强
- v0.15.0:定时任务 + VPS 资源监控 + CI/CD(GHCR 镜像发布)
- v0.14.0:TG Bot 增强(reip/open)+ 操作审计 + 新版本检查
- v0.13.0:OCI 对象存储管理 + 可开机器数检测
- v0.12.0:Telegram Bot 指令控制 + A1 配额体检
- v0.11.x:浅色主题;换IP联动CF DNS;域名&SSL监控;SSH 批量命令
- v0.10.0:性能大优化(并行扫描/SWR缓存/WAL/连接池/GZip)+ 稳定性修复
- v0.9.x:多云接入(Cloudflare/DNSHE/AWS)、Web SSH+SFTP、凭据库、手动VPS纳管、页面合并

</details>

## 🚀 快速开始

### 方式一:预构建镜像(最简)

```bash
docker run -d --name clouddeck -p 8080:8080 \
  -e PANEL_PASSWORD=yourpassword -v ./data:/app/data \
  ghcr.io/xiaoli8571/clouddeck:latest
```

> 若提示 manifest 不可见,请到 GitHub → 你的头像 → Packages → clouddeck → Package settings 改为 Public。

### 方式二:Docker Compose 自建

```bash
git clone https://github.com/xiaoli8571/clouddeck.git && cd clouddeck
PANEL_PASSWORD=yourpassword docker compose up -d
```

浏览器访问 `http://服务器IP:8080`。

### 方式三:本地运行

```bash
pip install -r requirements.txt
PANEL_PASSWORD=yourpassword python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 若不设置 `PANEL_PASSWORD`,首次启动会自动生成随机密码并打印在日志中,请及时查看并修改。

## ⚙️ 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PANEL_PASSWORD` | 自动生成 | 面板登录密码 |
| `PORT` | 8080 | 监听端口 |
| `GUARDIAN_INTERVAL` | 300 | 守护巡检间隔(秒,最小 60) |
| `INSTANCE_CACHE_TTL` | 30 | 实例总览缓存秒数(stale-while-revalidate) |
| `META_CACHE_TTL` | 120 | 创建实例表单元数据缓存秒数 |
| `MAX_WORKERS` | 8 | 云账户/实例并行扫描线程上限 |
| `COOKIE_SECURE` | auto | Cookie Secure 属性(auto/always/off) |

## 🔒 安全说明

- 云厂商 API 私钥:Fernet 加密落库(`data/master.key` 为密钥,**务必备份且勿泄露**)
- 面板会话:HMAC 签名 Cookie,7 天有效;失败防爆破锁定
- SSH 主机指纹:TOFU 记录于 `data/known_hosts.json`,变更即告警(MITM 检测)
- SSH/SFTP 凭据:仅存用户浏览器 localStorage 或加密凭据库
- 数据全部位于本机 `data/`(SQLite),备份该目录即完成迁移

## 🔑 如何获取云厂商凭证

- **Oracle Cloud**:控制台 → 头像 → 我的个人资料 → API 密钥 → 添加,记下 tenancy/user OCID、指纹并下载 PEM 私钥
- **AWS**:IAM 用户 → 创建访问密钥(Access Key ID / Secret);建议仅授予 EC2/Lightsail 必要权限
- **IBM Cloud**:Manage → Access (IAM) → API keys 创建;区域填如 `us-south` / `eu-de` / `jp-tok`
- **Cloudflare / DNSHE / HE.net**:对应控制台获取 API Token / 密钥

多区域管理:每个区域各添加一条账户记录即可。

## ❓常见问题

- **OCI 列表报 401/NotAuthenticated**:检查 tenancy/user OCID、指纹、私钥是否匹配
- **流量图表为空**:需启用 Compute Instance Monitoring 插件(面板有开关)
- **IBM 实例没有公网 IP**:IBM VPC 不会自动分配公网 IP,点「换IP」即可分配并绑定浮动 IP;排查接口:`GET /api/ibm/net-debug?account_id=&instance_id=`
- **AWS Lightsail 创建失败提示 availabilityZone**:v0.20.1 起已自动补全,创建表单也可手动指定
- **数据存放在哪**:`data/`(SQLite + 加密私钥 + 主密钥)

## 🆚 与 R探长(java_oci_manage)对比

| 能力 | CloudDeck | R探长 |
|---|---|---|
| OCI 实例/网络/卷/配额管理 | ✅ | ✅ |
| AWS EC2/Lightsail 管理 | ✅ 完整 | ✅ 更全 |
| IBM Cloud VPS 管理 | ✅ | ❌ |
| 创建实例强开 ARM(容量重试) | ✅ | ✅ |
| 换 IP / IPv6 / 保留 IP | ✅ | ✅ |
| 换 IP 自动更新 CF DNS | ✅ | ✅ |
| 流量统计 / 超限关停 / 停机保活 | ✅ | ✅ |
| 域名 & SSL 到期监控提醒 | ✅ | ✅ |
| Web SSH + SFTP + 凭据库 | ✅ | ✅ |
| SSH 批量命令 | ✅ | ✅ |
| 救援模式(启动盘跨实例挂载) | ✅ | ❌ |
| Cloudflare DNS/Workers/Pages | ✅ | ✅ 仅DNS |
| Telegram Bot 指令控制 | ✅ | ✅ 更全 |
| GCP/Azure/DO/SolusVM/VirtFusion | ❌ 暂不支持 | ✅ |
| 部署形态 | 单容器 · 无外部依赖 · 开源可审计 | 双端架构(TG Bot+客户端)· 闭源二进制 |

> 定位差异:CloudDeck 主打 **轻量自托管、代码全开源、单容器即起**;R探长功能更庞大但依赖其机器人服务端。适合想要"核心运维能力 + 数据完全自主"的场景。

## 🗺 Roadmap

- [ ] 串行控制台连接(OCI)
- [ ] 服务限额一键检测可开机器数(Limits API)
- [ ] 更多云:GCP / Vultr / DigitalOcean
- [ ] TG Bot 支持救援/创建指令
- [x] ~~救援系统~~ (v0.20.0)
- [x] ~~Telegram Bot 指令控制~~ (v0.12.0)
- [x] ~~A1 配额体检 + 一键降配~~ (v0.12.0)
- [x] ~~预留公网 IP 创建绑定 / 引导卷扩容 / VPU 调整~~ (v0.9.x)
- [x] ~~多区域聚合视图、Telegram/Bark 通知~~ (v0.9.x)

## ⚠️ 免责声明

本项目仅供学习研究与自有资源运维使用。请遵守各云服务商服务条款(尤其是 Oracle Cloud 免费层资源的使用政策),因使用本工具造成的账号封禁、资源损失等后果由使用者自行承担。
