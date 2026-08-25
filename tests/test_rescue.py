"""救援系统 mock 单测:不依赖 OCI SDK 与网络,验证 start/finish 编排与会话状态。

运行:python -m unittest tests.test_rescue -v
"""
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="rescue-test-")
os.environ["PANEL_PASSWORD"] = "unittest-password"
os.environ["RESCUE_POLL"] = "0"

from app import database, rescue  # noqa: E402
from app.security import init as security_init  # noqa: E402


# ---------------------------------------------------------------- Fakes

class _AnyModel:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ModelsNS:
    def __getattr__(self, name):
        return _AnyModel


class _CoreNS:
    models = _ModelsNS()


class FakeOci:
    core = _CoreNS()


class FakeResp:
    def __init__(self, data):
        self.data = data


def _ins(iid, name, ad, state):
    return SimpleNamespace(id=iid, display_name=name,
                           availability_domain=ad, lifecycle_state=state)


class FakeCompute:
    def __init__(self, bs=None):
        # id -> 实例
        self.instances = {
            "brk": _ins("brk", "broken-vm", "AD-1", "RUNNING"),
            "tgt": _ins("tgt", "rescue-vm", "AD-1", "RUNNING"),
        }
        self.boot_atts = [SimpleNamespace(id="BATT-1", instance_id="brk",
                                          boot_volume_id="BV-1",
                                          lifecycle_state="ATTACHED")]
        self.vol_atts = []
        self.bs = bs   # 联动卷状态
        self.seq = 0

    def _next(self, prefix):
        self.seq += 1
        return f"{prefix}-{self.seq}"

    def get_instance(self, iid):
        return FakeResp(self.instances[iid])

    def instance_action(self, iid, comp, action):
        if action in ("SOFTSTOP", "STOP"):
            self.instances[iid].lifecycle_state = "STOPPED"
            if self.bs:
                self.bs.state = "AVAILABLE"
        elif action == "START":
            self.instances[iid].lifecycle_state = "RUNNING"

    def list_boot_volume_attachments(self, **kw):
        rows = [a for a in self.boot_atts
                if a.instance_id == kw.get("instance_id")]
        return FakeResp(rows)

    def get_boot_volume_attachment(self, att_id):
        for a in self.boot_atts:
            if a.id == att_id:
                return FakeResp(a)
        raise RuntimeError("not found")

    def get_volume_attachment(self, att_id):
        for a in self.vol_atts:
            if a.id == att_id:
                return FakeResp(a)
        raise RuntimeError("not found")

    def detach_boot_volume(self, att_id):
        for i, a in enumerate(self.boot_atts):
            if a.id == att_id:
                self.boot_atts.pop(i)
                if self.bs:
                    self.bs.state = "AVAILABLE"

    def list_volume_attachments(self, comp, volume_id=None):
        rows = [a for a in self.vol_atts if a.volume_id == volume_id]
        return FakeResp(rows)

    def attach_volume(self, details):
        att = SimpleNamespace(id=self._next("VATT"), instance_id=details.instance_id,
                              volume_id=details.volume_id, lifecycle_state="ATTACHED")
        self.vol_atts.append(att)
        if self.bs:
            self.bs.state = "ATTACHED"
        return FakeResp(att)

    def attach_boot_volume(self, details):
        att = SimpleNamespace(id=self._next("BATT"), instance_id=details.instance_id,
                              boot_volume_id=details.boot_volume_id,
                              lifecycle_state="ATTACHED")
        self.boot_atts.append(att)
        if self.bs:
            self.bs.state = "ATTACHED"
        return FakeResp(att)

    def detach_volume(self, att_id):
        self.vol_atts = [a for a in self.vol_atts if a.id != att_id]
        if self.bs:
            self.bs.state = "AVAILABLE"


class FakeBS:
    def __init__(self):
        self.state = "ATTACHED"   # BV-1 的卷状态

    def get_boot_volume(self, bv_id):
        return FakeResp(SimpleNamespace(id=bv_id, lifecycle_state=self.state))


def _install_fakes(state_box):
    """把 rescue._clients 替换为返回假客户端。state_box: {'compute':..., 'bs':...}"""
    def _fake_clients(acct):
        return FakeOci(), state_box["compute"], state_box["bs"]
    rescue._clients = _fake_clients


# ---------------------------------------------------------------- 用例

class RescueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        security_init()
        database.init()
        with database.db() as c:
            c.execute(
                "INSERT INTO accounts(name,user_ocid,tenancy_ocid,region,fingerprint,"
                "private_key_enc,provider) VALUES('t','u','t','ap-seoul-1','f','x','oci')")
            cls.acct_id = c.execute("SELECT id FROM accounts").fetchone()["id"]

    def setUp(self):
        rescue._POLL = 0.01
        self.bs = FakeBS()
        self.compute = FakeCompute(bs=self.bs)
        self.box = {"compute": self.compute, "bs": self.bs}
        _install_fakes(self.box)
        self.acct = {"id": self.acct_id}

    def tearDown(self):
        with database.db() as c:
            c.execute("DELETE FROM rescue_sessions")

    def logs(self):
        out = []
        return out.append

    def test_start_rescue_happy_path(self):
        logs = []
        res = rescue.start_rescue(logs.append, self.acct, {
            "compartment_id": "comp1", "instance_id": "brk", "rescue_instance_id": "tgt"})
        sid = res["session_id"]
        # 故障实例已关机
        self.assertEqual(self.compute.instances["brk"].lifecycle_state, "STOPPED")
        # 原 boot 附件已移除,目标机上出现数据盘挂载
        self.assertFalse([a for a in self.compute.boot_atts if a.instance_id == "brk"])
        pv = [a for a in self.compute.vol_atts if a.volume_id == "BV-1"]
        self.assertTrue(pv and pv[0].instance_id == "tgt")
        # 会话记录
        sess = rescue.get_session(sid)
        self.assertEqual(sess["status"], "rescuing")
        self.assertEqual(sess["instance_name"], "broken-vm")
        self.assertEqual(sess["rescue_instance_name"], "rescue-vm")
        # 保活排除集合包含故障实例
        self.assertIn("brk", rescue.rescuing_instance_ids())
        # 日志含修复指引
        joined = "\n".join(str(x) for x in logs)
        self.assertIn("lsblk", joined)
        self.assertIn("umount", joined)

    def test_start_rejects_cross_ad_and_stopped_target(self):
        self.compute.instances["tgt"].availability_domain = "AD-2"
        with self.assertRaises(rescue.OciError):
            rescue.start_rescue(lambda m: None, self.acct,
                                {"compartment_id": "c", "instance_id": "brk",
                                 "rescue_instance_id": "tgt"})
        self.compute.instances["tgt"].availability_domain = "AD-1"
        self.compute.instances["tgt"].lifecycle_state = "STOPPED"
        with self.assertRaises(rescue.OciError):
            rescue.start_rescue(lambda m: None, self.acct,
                                {"compartment_id": "c", "instance_id": "brk",
                                 "rescue_instance_id": "tgt"})

    def test_finish_rescue_happy_path(self):
        logs = []
        res = rescue.start_rescue(logs.append, self.acct, {
            "compartment_id": "comp1", "instance_id": "brk", "rescue_instance_id": "tgt"})
        sid = res["session_id"]
        logs2 = []
        out = rescue.finish_rescue(logs2.append, self.acct, sid)
        # 原实例重新开机,数据盘挂载已清空,boot 附件回到 brk
        self.assertEqual(self.compute.instances["brk"].lifecycle_state, "RUNNING")
        self.assertFalse(self.compute.vol_atts)
        boots = [a for a in self.compute.boot_atts if a.instance_id == "brk"]
        self.assertTrue(boots)
        sess = rescue.get_session(sid)
        self.assertEqual(sess["status"], "restored")
        self.assertEqual(out["instance_id"], "brk")
        # 还原后不再占用保活排除集合
        self.assertNotIn("brk", rescue.rescuing_instance_ids())

    def test_finish_rejects_running_original(self):
        res = rescue.start_rescue(lambda m: None, self.acct, {
            "compartment_id": "c", "instance_id": "brk", "rescue_instance_id": "tgt"})
        # 模拟原实例被外部方式拉起
        self.compute.instances["brk"].lifecycle_state = "RUNNING"
        with self.assertRaises(rescue.OciError):
            rescue.finish_rescue(lambda m: None, self.acct, res["session_id"])

    def test_forget_session(self):
        res = rescue.start_rescue(lambda m: None, self.acct, {
            "compartment_id": "c", "instance_id": "brk", "rescue_instance_id": "tgt"})
        rescue.forget_session(res["session_id"])
        self.assertIsNone(rescue.get_session(res["session_id"]))
        self.assertEqual(rescue.list_sessions(), [])

    def test_rescue_meta_filters_targets(self):
        from app import oci_client as oc
        rows = [
            {"id": "brk", "name": "broken", "state": "STOPPED", "ad": "AD-1"},
            {"id": "ok1", "name": "good", "state": "RUNNING", "ad": "AD-1",
             "public_ip": "1.2.3.4", "private_ip": "10.0.0.5", "shape": "VM.Standard.A1.Flex",
             "ocpus": 2, "mem_gbs": 12, "compartment_name": "root"},
            {"id": "ok2", "name": "other-ad", "state": "RUNNING", "ad": "AD-2",
             "public_ip": None, "private_ip": None, "shape": "", "ocpus": None,
             "mem_gbs": None, "compartment_name": "root"},
            {"id": "off", "name": "off", "state": "STOPPED", "ad": "AD-1",
             "public_ip": None, "private_ip": None, "shape": "", "ocpus": None,
             "mem_gbs": None, "compartment_name": "root"},
        ]
        orig = oc.list_instances
        oc.list_instances = lambda acct: rows
        try:
            d = rescue.rescue_meta(self.acct, "c", "brk")
        finally:
            oc.list_instances = orig
        self.assertEqual(d["instance"]["id"], "brk")
        ids = [t["id"] for t in d["targets"]]
        self.assertEqual(ids, ["ok1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
