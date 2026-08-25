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
    """v0.20.2:三数据源交叉补全(区域浮动IP表 / 单网卡详情 / 主网卡ID兜底)。"""

    def setUp(self):
        self.acct = {"id": 2, "name": "ibm", "region": "jp-tok"}
        self.orig_req = ibm_cloud._req

    def tearDown(self):
        ibm_cloud._req = self.orig_req

    def _patch(self, routes):
        """routes: {(method, path): payload_or_exc},精确匹配;按序首个命中生效。"""
        import json as _json

        def fake_req(acct, method, path, *, params=None, json_body=None,
                     retry_auth=True):
            for m, p in routes:
                if m == method and p == path:
                    item = routes[(m, p)]
                    if isinstance(item, Exception):
                        raise item
                    return _json.loads(_json.dumps(item))   # 深拷贝
            raise AssertionError(f"unexpected {method} {path}")
        ibm_cloud._req = fake_req

    def test_full_flow_public_and_private_ip(self):
        self._patch({
            ("GET", "/instances/i1/network_interfaces/nic1"): {
                "id": "nic1", "primary_ip": {"address": "10.240.0.5"},
                "floating_ip": {"id": "fip1", "address": "133.18.100.7"}},
            ("GET", "/instances/i1"): {
                "primary_network_interface": {"id": "nic1"}, "status": "running"},
            ("GET", "/floating_ips"): {"floating_ips": [
                {"id": "fip1", "address": "133.18.100.7",
                 "target": {"id": "nic1"}}]},
            ("GET", "/instances"): {"instances": []},
        })
        rows = [{"id": "i1", "name": "vm-a", "vnic_id": "nic1",
                 "public_ip": None, "private_ip": None, "public_lifetime": None}]
        ibm_cloud._enrich_network(self.acct, rows)
        r = rows[0]
        self.assertEqual(r["public_ip"], "133.18.100.7")
        self.assertEqual(r["_fip_id"], "fip1")
        self.assertEqual(r["public_lifetime"], "RESERVED")
        self.assertEqual(r["private_ip"], "10.240.0.5")

    def test_no_floating_ip_attached_stays_empty(self):
        """实例没绑浮动 IP(IBM VPC 不自动分配)→ 公网 IP 为空是正确行为。"""
        self._patch({
            ("GET", "/instances/i2"): {
                "primary_network_interface": {"id": "nic9"}, "status": "running"},
            ("GET", "/instances/i2/network_interfaces/nic9"): {
                "id": "nic9", "primary_ip": {"address": "10.240.0.6"}},
            ("GET", "/floating_ips"): {"floating_ips": []},
            ("GET", "/instances"): {"instances": []},
        })
        rows = [{"id": "i2", "name": "vm-b", "vnic_id": "",
                 "public_ip": None, "private_ip": None, "public_lifetime": None}]
        ibm_cloud._enrich_network(self.acct, rows)
        self.assertIsNone(rows[0]["public_ip"])
        self.assertEqual(rows[0]["private_ip"], "10.240.0.6")

    def test_region_map_used_when_detail_lacks_fip(self):
        """单网卡详情没给 floating_ip 时,用区域浮动 IP 表兜底。"""
        self._patch({
            ("GET", "/instances/i3/network_interfaces/nic3"): {
                "id": "nic3", "primary_ip": {"address": "10.240.0.7"}},
            ("GET", "/floating_ips"): {"floating_ips": [
                {"id": "fipZ", "address": "150.x.y.z",
                 "target": {"id": "nic3"}}]},
            ("GET", "/instances"): {"instances": []},
        })
        rows = [{"id": "i3", "name": "vm-c", "vnic_id": "nic3",
                 "public_ip": None, "private_ip": None, "public_lifetime": None}]
        ibm_cloud._enrich_network(self.acct, rows)
        self.assertEqual(rows[0]["public_ip"], "150.x.y.z")
        self.assertEqual(rows[0]["_fip_id"], "fipZ")
        self.assertEqual(rows[0]["private_ip"], "10.240.0.7")

    def test_detail_failure_still_uses_map(self):
        """网卡详情接口挂掉,区域浮动 IP 表仍能给出公网 IP。"""
        from app.pcreds import ProviderError as PE
        self._patch({
            ("GET", "/instances/i4/network_interfaces"): PE("nic detail down"),
            ("GET", "/floating_ips"): {"floating_ips": [
                {"id": "fipW", "address": "1.2.3.4",
                 "target": {"id": "nic4"}}]},
            ("GET", "/instances"): {"instances": []},
        })
        rows = [{"id": "i4", "name": "vm-d", "vnic_id": "nic4",
                 "public_ip": None, "private_ip": None, "public_lifetime": None}]
        ibm_cloud._enrich_network(self.acct, rows)
        self.assertEqual(rows[0]["public_ip"], "1.2.3.4")

    def test_list_instances_end_to_end(self):
        """完整链路:列表 → 补全(主网卡缺失时经实例详情兜底)。"""
        calls = []

        def fake_req(acct, method, path, *, params=None, json_body=None,
                     retry_auth=True):
            calls.append((method, path))
            if path == "/instances":
                return {"instances": [
                    {"id": "e1", "name": "vm-e", "status": "running"}]}
            if path == "/floating_ips":
                return {"floating_ips": [
                    {"id": "f", "address": "9.9.9.9",
                     "target": {"id": "nicE"}}]}
            if path == "/instances/e1":
                return {"primary_network_interface": {"id": "nicE"}}
            if path == "/instances/e1/network_interfaces/nicE":
                return {"id": "nicE", "primary_ip": {"address": "10.0.0.1"}}
            raise AssertionError(path)

        ibm_cloud._req = fake_req
        rows = ibm_cloud.list_instances(self.acct)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["public_ip"], "9.9.9.9")
        self.assertEqual(rows[0]["private_ip"], "10.0.0.1")
        self.assertIn(("GET", "/instances"), calls)

    def test_fetch_nics_pagination(self):
        def fake_req(acct, method, path, *, params=None, json_body=None,
                     retry_auth=True):
            if params.get("start"):
                return {"network_interfaces": [{"id": "n2"}]}
            return {"network_interfaces": [{"id": "n1"}],
                    "next": {"href": f"{path}?start=ABC"}}

        ibm_cloud._req = fake_req
        nics = ibm_cloud._fetch_nics(self.acct, "ix")
        self.assertEqual([n["id"] for n in nics], ["n1", "n2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
