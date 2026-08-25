"""请求体模型。"""
from pydantic import BaseModel, Field


class LoginReq(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class AccountIn(BaseModel):
    """多提供商账户:provider 决定哪些字段必填。"""
    provider: str = "oci"            # oci / aws / cloudflare / dnshe
    name: str = Field(min_length=1, max_length=64)
    region: str = ""                 # oci/aws 使用
    tenancy_ocid: str = ""
    user_ocid: str = ""
    fingerprint: str = ""
    private_key: str = ""            # PEM;编辑时留空表示不修改
    aws_access_key_id: str = ""
    aws_secret_key: str = ""
    ibm_api_key: str = ""            # IBM Cloud IAM API Key
    cf_token: str = ""
    he_email: str = ""
    he_pass: str = ""
    he_api_secret: str = ""
    dnshe_api_key: str = ""
    dnshe_api_secret: str = ""


class OpReq(BaseModel):
    account_id: int
    compartment_id: str
    instance_id: str
    op: str   # START / SOFTSTOP / STOP / RESET / SOFTRESET


class ChangeIpReq(BaseModel):
    account_id: int
    compartment_id: str
    instance_id: str
    # 可选:换 IP 完成后自动更新 Cloudflare DNS A 记录(R探长同款联动)
    dns_update: "DnsUpdate | None" = None


class DnsUpdate(BaseModel):
    """换 IP 后 DNS 联动参数。cf_account_id 为面板中的 cloudflare 账户 id。"""
    cf_account_id: int
    zone: str = Field(min_length=1, max_length=255)          # 域名(zone name)或 zone_id
    record_name: str = Field(min_length=1, max_length=255)   # 如 panel 或 @
    proxied: bool = False


ChangeIpReq.model_rebuild()


class TrafficReq(BaseModel):
    account_id: int
    compartment_id: str


class CreateInstanceReq(BaseModel):
    account_id: int
    compartment_id: str
    name: str = Field(min_length=1, max_length=60)
    ad: str
    subnet_id: str
    image_id: str
    shape_kind: str              # amd / arm / custom
    ocpus: float | None = None
    mem_gbs: float | None = None
    boot_gbs: int = 50
    ssh_key: str = ""
    retry_attempts: int = 1
    retry_delay: int = 45


class RenameReq(BaseModel):
    account_id: int
    instance_id: str
    display_name: str = Field(min_length=1, max_length=80)


class ResizeReq(BaseModel):
    account_id: int
    instance_id: str
    ocpus: float = Field(gt=0, le=32)
    mem_gbs: float = Field(gt=0, le=512)


class TerminateReq(BaseModel):
    account_id: int
    instance_id: str
    preserve_boot_volume: bool = False


class NetRef(BaseModel):
    account_id: int
    compartment_id: str
    instance_id: str


class ReservedIpOp(BaseModel):
    account_id: int
    compartment_id: str
    op: str          # create / delete / bind / unbind
    public_ip_id: str = ""
    vnic_id: str = ""


class PortsReq(NetRef):
    ports: list[int] = Field(min_length=1, max_length=20)


class VolumeUpdateReq(BaseModel):
    account_id: int
    boot_volume_id: str
    size_in_gbs: int | None = Field(default=None, ge=47, le=4096)
    vpus_per_gb: int | None = None


class RescueStartReq(BaseModel):
    """发起实例救援:把故障实例启动盘挂到 rescue_instance_id 上修复。"""
    account_id: int
    compartment_id: str
    instance_id: str
    rescue_instance_id: str


class RescueFinishReq(BaseModel):
    session_id: int


class GuardianRule(BaseModel):
    account_id: int
    enabled: bool = False
    keepalive: bool = False
    traffic_limit_gb: float = Field(default=0, ge=0, le=100000)
    traffic_action: str = "notify"


class WebhookReq(BaseModel):
    webhook_url: str = Field(default="", max_length=500)


# ---------- Cloudflare ----------

class CfZoneRef(BaseModel):
    account_id: int
    zone_id: str


class CfRecordIn(CfZoneRef):
    type: str = Field(min_length=1, max_length=10)
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=4096)
    ttl: int = 300
    proxied: bool = False
    priority: int | None = None


class CfRecordUpd(CfRecordIn):
    record_id: str


class WorkerDeploy(BaseModel):
    account_id: int
    cf_account_id: str = ""
    name: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=1024 * 1024)


class TgReq(BaseModel):
    bot_token: str = Field(default="", max_length=200)
    chat_id: str = Field(default="", max_length=64)
    enabled: bool = False   # 是否启用 Telegram Bot 指令控制


class PasswordReq(BaseModel):
    old: str = Field(min_length=1, max_length=128)
    new: str = Field(min_length=6, max_length=128)


class HeRecordIn(BaseModel):
    account_id: int
    zone_id: str
    name: str = Field(min_length=0, max_length=255)
    type: str = Field(min_length=1, max_length=10)
    content: str = Field(min_length=1, max_length=4096)
    ttl: int = 86400


class CfRouteIn(BaseModel):
    account_id: int
    zone_id: str
    pattern: str = Field(min_length=1, max_length=255)
    script: str = Field(min_length=1, max_length=64)


class HeDdnsReq(BaseModel):
    account_id: int
    hostname: str = Field(min_length=1, max_length=255)
    secret: str = Field(min_length=1, max_length=255)
    ip: str = Field(default="", max_length=64)


class DnsheSubdomainIn(BaseModel):
    account_id: int
    subdomain: str = Field(min_length=1, max_length=63)
    rootdomain: str = Field(min_length=1, max_length=255)


class DnsheRecordIn(BaseModel):
    account_id: int
    subdomain_id: int
    type: str = Field(min_length=1, max_length=10)
    name: str = Field(default="", max_length=255)
    content: str = Field(default="", max_length=4096)
    ttl: int = 600
    priority: int | None = None


class DnsheRecordUpd(DnsheRecordIn):
    record_id: str = ""


# ---------- 手动添加的 VPS 主机 ----------

class VpsHostIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    auth_type: str = Field(default="password")   # password / key
    secret: str = ""                              # 密码或 PEM 私钥;编辑时留空表示不修改
    region: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=200)


# ---------- OCI 对象存储 ----------

class OssBucketIn(BaseModel):
    account_id: int
    name: str = Field(min_length=3, max_length=63)   # 小写字母/数字/._-
    tier: str = Field(default="Standard")             # Standard / Archive


class OssObjectIn(BaseModel):
    account_id: int
    bucket: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=1024)
    content_b64: str = ""                             # 上传内容(base64);≤50MB
