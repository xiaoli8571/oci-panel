"""v0.20.1 修复回归测试:

1) AWS Lightsail create_instances 必填 availabilityZone —— 留空时自动取该区域
   第一个可用区,彻底解决 Missing required parameter in input: "availabilityZone"
2) IBM /instances 列表接口不含浮动 IP —— 逐实例拉取网卡详情补全 公网/私网 IP

运行:python -m unittest tests.test_fixes -v
"""
import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fixes-test-"))

from app import aws_cloud, ibm_cloud  # noqa: E402
from app.pcreds import ProviderError  # noqa: E402


# ---------------------------------------------------------------- AWS Lightsail AZ

class FakeLS:
    def __init__(self, regions):
        self._regions = regions
        self.calls = []

    def get_regions(self, **kw):
        return {"regions": self._regions}

    def create_instances(self, **kw):
        self.calls.append(kw)
        return {"operations": [{"status": "Started"}]}


class TestLightsailAz(unittest.TestCase):
    def setUp(self):
        self.acct = {"id": 1}
        self.ls = FakeLS([
            {"name": "ap-northeast-1",
             "availabilityZones": [
                 {"state": "available", "zoneName": "ap-northeast-1a"},
                 {"state": "available", "zoneName": "ap-northeast-1d"}]},
            {"name": "us-east-1",
             "availabilityZones": [
                 {"state": "available", "zoneName": "us-east-1b"}]},
        ])
        self.orig = aws_cloud._ls
        aws_cloud._ls = lambda acct_, region=None: self.ls

    def tearDown(self):
        aws_cloud._ls = self.orig

    def test_blank_az_autofilled_with_first_zone(self):
        out = aws_cloud.lightsail_create(self.acct, "ap-northeast-1", "web", "os", "bundle", "")
        self.assertEqual(out["az"], "ap-northeast-1a")
        self.assertEqual(self.ls.calls[0]["availabilityZone"], "ap-northeast-1a")

    def test_explicit_az_kept(self):
        out = aws_cloud.lightsail_create(self.acct, "us-east-1", "web", "os", "bundle", "")
        self.assertEqual(out["az"], "us-east-1b")
        self.assertEqual(self.ls.calls[0]["availabilityZone"], "us-east-1b")

    def test_az_always_sent(self):
        """任何情况下 create_instances 都必须带 availabilityZone(必填参数)。"""
        aws_cloud.lightsail_create(self.acct, "ap-northeast-1", "w2", "os", "bundle", "")
        for call in self.ls.calls:
            self.assertIn("availabilityZone", call)

    def test_no_zones_raises_clear_error(self):
        self.ls._regions = [{"name": "ap-northeast-1", "availabilityZones": []}]
        with self.assertRaises(ProviderError) as cm:
            aws_cloud.lightsail_create(self.acct, "ap-northeast-1", "web", "os", "bundle", "")
        self.assertIn("可用区", str(cm.exception))

    def test_meta_contains_zones(self):
        class MetaLS(FakeLS):
            def get_blueprints(self):
                return {"blueprints": []}

            def get_bundles(self):
                return {"bundles": []}

        self.ls.__class__ = MetaLS
        d = aws_cloud.lightsail_meta(self.acct, "ap-northeast-1")
        self.assertEqual(d["zones"], ["ap-northeast-1a", "ap-northeast-1d"])


# ---------------------------------------------------------------- IBM 浮动 IP 补全

class TestIbmEnrich(unittest.TestCase):
    def setUp(self):
        self.acct = {"id": 2, "name": "ibm", "region": "jp-tok"}
        self.orig_req = ibm_cloud._req

    def tearDown(self):
        ibm_cloud._req = self.orig_req

    def _patch_req(self, list_payload, nic_payloads):
        def fake_req(acct, method, path, *, params=None, json_body=None,
                     retry_auth=True):
            if path == "/instances":
                return dict(list_payload)
            if path.startswith("/instances/") and path.endswith("/network_interfaces"):
                iid = path.split("/")[2]
                return dict(nic_payloads[iid])
            raise AssertionError(f"unexpected path {path}")
        ibm_cloud._req = fake_req

    def test_list_enriches_floating_and_private_ip(self):
        self._patch_req(
            {"instances": [
                {"id": "i1", "name": "vm-a", "status": "running",
                 "primary_network_interface": {"id": "nic1"},
                 "network_interfaces": [{"id": "nic1"}]},
                {"id": "i2", "name": "vm-b", "status": "running"},
            ]},
            {"i1": {"network_interfaces": [
                {"id": "nic1", "primary_ip": {"address": "10.240.0.5"},
                 "floating_ip": {"id": "fip1", "address": "133.18.100.7"}}]},
             "i2": {"network_interfaces": [
                 {"id": "nic9", "primary_ip": {"address": "10.240.0.6"}}]}},
        )
        rows = ibm_cloud.list_instances(self.acct)
        a = next(r for r in rows if r["id"] == "i1")
        b = next(r for r in rows if r["id"] == "i2")
        self.assertEqual(a["public_ip"], "133.18.100.7")
        self.assertEqual(a["_fip_id"], "fip1")
        self.assertEqual(a["public_lifetime"], "RESERVED")
        self.assertEqual(a["private_ip"], "10.240.0.5")
        self.assertIsNone(b["public_ip"])          # 无浮动 IP → 不显示
        self.assertEqual(b["private_ip"], "10.240.0.6")

    def test_enrich_failure_does_not_break_list(self):
        def failing_req(acct, method, path, *, params=None, json_body=None,
                        retry_auth=True):
            if path == "/instances":
                return {"instances": [
                    {"id": "i9", "name": "vm-x", "status": "running",
                     "network_interfaces": []}]}
            raise RuntimeError("nic api down")
        ibm_cloud._req = failing_req
        rows = ibm_cloud.list_instances(self.acct)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["public_ip"])

    def test_nic_pagination(self):
        calls = []

        def paginated(acct, method, path, *, params=None, json_body=None,
                      retry_auth=True):
            if path == "/instances":
                return {"instances": [
                    {"id": "ip", "name": "vm-p", "status": "running"}]}
            calls.append(params.get("start"))
            if not params.get("start"):
                return {"network_interfaces": [{"id": "n1"}],
                        "next": {"href": f"{path}?start=ABC"}}
            return {"network_interfaces": [
                {"id": "n2", "floating_ip": {"id": "f", "address": "1.2.3.4"}}]}

        ibm_cloud._req = paginated
        rows = ibm_cloud.list_instances(self.acct)
        self.assertEqual(rows[0]["public_ip"], "1.2.3.4")
        self.assertEqual(calls, [None, "ABC"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
