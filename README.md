# OCI Manage Lite · Oracle Cloud 轻量管理面板

仿照 [semicons/java_oci_manage](https://github.com/semicons/java_oci_manage)(R探长)实现的 **Oracle Cloud (OCI) 网页管理面板**,使用 Python (FastAPI) + 原生前端实现,单文件部署、无外部数据库依赖。

## 🚦 更新日志

### v0.11.0(对标 R探长:自动化运维三件套)

- 🔁 **换 IP 自动更新 Cloudflare DNS**(R探长同款联动):换 IP 弹窗可选填「域名 记录名」,新 IP 生效后自动更新/创建 A 记录,任务日志可见结果
- 🌐 **域名 & SSL 证书到期监控**:守护中心新增监控清单;每 6 小时经 RDAP 查域名注册到期、TLS 握手读证书到期;剩余 ≤30/14/7/3/1 天分档推送 Telegram/Webhook(每档每日最多提醒一次);支持一键立即检测
- ⚡ **SSH 批量命令**:实例页新增「批量命令」;候选主机自动汇总手动 VPS + 凭据库已保存凭据,多台并行执行、逐台展示退出码与输出(截断 8KB)

### v0.10.0(性能优化 + 稳定性修复)

**性能**
- 实例总览:多账户并行扫描;单实例 VNIC/启动盘/保留IP 判定并行化,大账户列表提速数倍
- 缓存升级 stale-while-revalidate:过期请求秒回旧数据 + 后台自动刷新(`?refresh=1` 强制刷新);`INSTANCE_CACHE_TTL` 可调
- OCI SDK / boto3 / Lightsail 客户端缓存复用(免重复解密私钥、解析 PEM、构建客户端)
- 创建实例表单元数据(区间/AD/镜像/子网)TTL 缓存(`META_CACHE_TTL`)
- 配额查询、流量指标、CF Workers 列表等独立 API 调用并行发出
- SQLite 启用 WAL 模式 + 连接复用,Web 与守护线程并发不再互相阻塞;新增事件表索引
- HTTP 连接池(Cloudflare/DNSHE/Telegram/Webhook 复用 TLS 连接)
- 响应 GZip 压缩(首页 125KB→32KB);静态资源浏览器缓存 24h;xterm.js 改为首次打开 SSH 时懒加载

**修复**
- 🔴 「一键开放端口」因变量未定义必然报错的 bug
- 🔴 Web SSH 首次连接确认指纹后未保存,导致每次都弹确认框(TOFU 记录失效)
- 换 IP 后立即读取公网 IP 可能为空,现轮询等待分配
- 任务日志加长度上限与并发快照,长任务不再撑爆内存
- 登录防爆破记录定期清理;Cookie 支持 `COOKIE_SECURE=auto`(HTTPS 自动加 Secure)

**运维**
- 新增 `/healthz` 健康检查;Dockerfile 与 docker-compose 内置 HEALTHCHECK

### 历史

- v0.9.3:修复 CF 删除记录 405(record_id 字段映射)+ SSH/SFTP 弹窗自动带出已存用户名
- v0.9.2:SSH/SFTP 凭据可保存(加密凭据库),云实例免重复输密码
- v0.9.1:手动添加 VPS 统一纳管 + SSH/SFTP 融入实例行 + 页面大合并(7页→4页)
- v0.9.0:多云管理面板(OCI/AWS/Cloudflare/DNSHE), Web SSH 轻量版, CF Git 部署

## ✨ 功能

| 功能 | 说明 |
|---|---|
| 🔐 面板登录 | 密码登录(HMAC 会话 Cookie),失败防爆破锁定 |
| 👤 多账户管理 | 支持添加多个 OCI 账户 / 区域;API 私钥 **加密后** 存储在本机 |
| 🖥 实例总览 | 自动枚举所有区间(Compartment)中的实例:状态、公网 IP(临时/保留标识)、私网 IP、OCPU/内存、启动盘大小 |
| 🚀 创建实例 | AMD 微型 1C/1G、ARM A1 一键预设;镜像/子网/AD 可选;**强开 ARM**(容量不足自动重试,次数/间隔可调),任务窗口实时日志 |
| ⏻ 电源操作 | 启动 / 软关机 / 强制关机 / 重启 |
| 🔄 IP 管理 | 一键换临时 IP(自动关开机);**保留公网 IP** 新建/删除/绑定/解绑;IPv6 一键附加 |
| 🔓 端口开放 | 一键向安全列表追加 TCP 入站规则(22/80/443/自定义,IPv4+IPv6 全网段) |
| 💽 卷管理 | 启动盘扩容、VPU 性能层级调整(Balanced/Better/Ultra) |
| 🔧 规格与属性 | 升降配(A1.Flex OCPU/内存)、实例改名、终止(可选保留启动盘) |
| 📊 配额订阅 | A1 核数/内存、E2.Micro 数量在各 AD 的用量与余量;免费层/PAYG 订阅类型识别 |
| 📈 流量统计 | `oci_computeagent` 监控指标,24 小时 / 7 天 / 30 天切换,聚合粒度自适应 |
| 🛡 守护中心 | 停机自动拉起(保活);月流量超限通知 / 自动关停(被关停账号当月不保活);事件历史;Webhook 通知(Telegram/Bark/Server酱,支持 `{msg}` 占位) |
| 💻 Web SSH | 浏览器多标签终端(xterm.js),密码/私钥认证,主机指纹 TOFU 校验,云主机一键导入;凭据仅存浏览器 |
| 📁 SFTP 文件管理 | 浏览/上传/下载/重命名/删除/新建目录,单文件上限 50MB |

## 🚀 快速开始

### 方式一:Docker(推荐)

```bash
git clone <本项目> && cd oci-panel
PANEL_PASSWORD=yourpassword docker compose up -d
```

浏览器访问 `http://服务器IP:8080`。

### 方式二:本地运行

```bash
pip install -r requirements.txt
PANEL_PASSWORD=yourpassword python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 若不设置 `PANEL_PASSWORD`,首次启动会自动生成随机密码并打印在日志中,请及时查看并修改。

### 方式三:服务器常驻(systemd)

```bash
# 已在目标服务器 /opt/oci-panel 部署时可使用 systemd 管理
docker compose -f /opt/oci-panel/docker-compose.yml up -d --build
```

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

- OCI API 私钥:Fernet 加密落库(`data/master.key` 为密钥,**务必备份且勿泄露**)
- 面板会话:HMAC 签名 Cookie,7 天有效
- SSH 主机指纹:首次连接确认后记录于 `data/known_hosts.json`,变更即告警(MITM 检测)
- SSH/SFTP 凭据:只保存在用户浏览器 localStorage 或会话内存,面板服务器不存储

## 🔑 如何获取 OCI API 凭证

1. 登录 [OCI 控制台](https://cloud.oracle.com) → 右上角头像 → **我的个人资料 / User Settings**
2. 左侧 **API 密钥(API Keys)** → **添加 API 密钥**,生成或上传密钥后记下:
   - 指纹(Fingerprint)
   - 租户 OCID(Tenancy OCID)
   - 用户 OCID(User OCID)
   - 下载的私钥 PEM 文件内容
3. 在面板 **账户设置 → 添加账户** 中填入以上信息与目标区域(如 `ap-seoul-1`)。

> 同一账号要管理多个区域时,为每个区域各添加一条记录即可。
> 建议为该 API Key 的用户仅授予必要的 IAM 策略权限。

## ❓常见问题

- **列表加载报 401/NotAuthenticated**:检查 tenancy/user OCID、指纹、私钥是否匹配,以及用户是否被解锁。
- **流量图表为空**:需在实例中启用 Oracle Cloud Agent 的 *Compute Instance Monitoring* 插件。
- **更换 IP 后连不上**:新 IP 已在任务日志中显示;若使用保留 IP 请先解绑。
- **数据存放在哪**:全部位于 `data/`(SQLite + 加密私钥 + 主密钥),备份该目录即完成迁移。⚠️ 请勿泄露 `data/master.key`。

## 🆚 与 R探长(java_oci_manage)对比

| 能力 | oci-panel | R探长 |
|---|---|---|
| OCI 实例/网络/卷/配额管理 | ✅ | ✅ |
| 创建实例强开 ARM(容量重试) | ✅ | ✅ |
| 换 IP / IPv6 / 保留 IP | ✅ | ✅ |
| 换 IP 自动更新 CF DNS | ✅ v0.11 | ✅ |
| 流量统计 / 超限关停 / 停机保活 | ✅ | ✅ |
| 域名 & SSL 到期监控提醒 | ✅ v0.11 | ✅ |
| Web SSH + SFTP | ✅ | ✅ |
| SSH 批量命令 | ✅ v0.11 | ✅ |
| AWS EC2/Lightsail 管理 | ✅ 基础 | ✅ 更全 |
| Cloudflare DNS/Workers | ✅ | ✅ DNS |
| 部署形态 | 单容器 · 无外部依赖 · 开源可审计 | 双端架构(TG Bot+客户端)· 闭源二进制 |
| Telegram Bot 全功能控制 | 🗺 规划中 | ✅ |
| GCP/Azure/DO/SolusVM/VirtFusion | ❌ 暂不支持 | ✅ |
| 串行控制台 / 对象存储 / ACME 证书 | ❌ 暂不支持 | ✅ |
| A1 配额体检与降配抢占 | 部分(配额查询已有) | ✅ |

> 定位差异:oci-panel 主打 **轻量自托管、代码全开源、单容器即起**;R探长功能更庞大但依赖其机器人服务端。适合想要"核心运维能力 + 数据完全自主"的场景。

## 🗺 Roadmap

- [ ] Telegram Bot 指令控制(查询/开关机/换IP)
- [ ] A1 配额体检 + 一键降配抢占
- [ ] OCI 对象存储(Object Storage)管理
- [ ] 串行控制台连接
- [ ] 服务限额(Limits)一键检测可开机器数
- [ ] 多用户与操作审计日志
- [x] ~~预留公网 IP 的创建与绑定~~ (v0.9.x 已完成)
- [x] ~~引导卷扩容 / 更改 VPUs 性能层级~~ (v0.9.x 已完成)
- [x] ~~多区域聚合视图、Telegram/Bark 通知~~ (v0.9.x 已完成)

## ⚠️ 免责声明

本项目仅供学习研究与自有资源运维使用。请遵守 Oracle Cloud 服务条款(尤其是免费层资源的使用政策),因使用本工具造成的账号封禁、资源损失等后果由使用者自行承担。

## License

MIT
