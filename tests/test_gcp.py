"""Google Cloud (GCE) 接入 mock 单测。

运行:python -m unittest tests.test_gcp -v
"""
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="gcp-test-"))
os.environ["GCP_POLL"] = "0"

from app import gcp_cloud  # noqa: E402
from app.pcreds import ProviderError  # noqa: E402


def _ins_payload(name, status="RUNNING", nat_ip="34.1.2.3", pri="10.0.0.5",
                 zone="us-central1-a", mt="e2-small", iid=12345, boot=10):
    return {
        "id": iid, "name": name, "status": status,
        "machineType": f"https://.../machineTypes/{mt}",
        "zone": f"https://.../zones/{zone}",
        "creationTimestamp": "2026-08-25T03:00:00.000-07:00",
        "networkInterfaces": [{
            "name": "nic0", "networkIP": pri,
            "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT",
                               "natIP": nat_ip}] if nat_ip else [],
            "disks": [],
        }],
        "disks": [{"boot": True, "diskSizeGb": boot}],
    }


class GcpTest(unittest.TestCase):
    def setUp(self):
        self.acct = {"id": 9, "name": "gcp-main"}
        self.orig_req = gcp_cloud._req
        self.orig_token = gcp_cloud._token
        self.orig_proj = gcp_cloud._project

        def fake_token(acct):
            return "fake-token"
        gcp_cloud._token = fake_token
        gcp_cloud._project = lambda acct, info=None: "test-project"
        self.calls = []

    def tearDown(self):
        gcp_cloud._req = self.orig_req
        gcp_cloud._token = self.orig_token
        gcp_cloud._project = self.orig_proj

    def _patch(self, handler):
        """handler(method, path, params, json_body) -> dict"""
        def req(acct, method, path, *, params=None, json_body=None, retry_auth=True):
            self.calls.append((method, path))
            return handler(method, path, params or {}, json_body)
        gcp_cloud._req = req

    def test_list_instances_aggregated(self):
        def h(method, path, params, body):
            assert path.endswith("/aggregated/instances")
            page = params.get("pageToken")
            if not page:
                return {"items": {
                    "zones/us-central1-a": {"instances": [
                        _ins_payload("vm-a"),
                        _ins_payload("vm-b", status="TERMINATED", nat_ip="", iid=2)]},
                    "zones/us-east1-b": {"instances": [_ins_payload("vm-c", iid=3)]},
                    "zones/us-west1-a": {"warnings": [{"code": "NO_RESULTS_ON_PAGE"}]},
                }, "nextPageToken": "P2"}
            return {"items": {"zones/europe-west1-d": {"instances": [
                _ins_payload("vm-eu", iid=4)]}}}

        self._patch(h)
        rows = gcp_cloud.list_instances(self.acct)
        names = sorted(r["name"] for r in rows)
        self.assertEqual(names, ["vm-a", "vm-b", "vm-c", "vm-eu"])
        a = next(r for r in rows if r["name"] == "vm-a")
        self.assertEqual(a["public_ip"], "34.1.2.3")
        self.assertEqual(a["private_ip"], "10.0.0.5")
        self.assertEqual(a["state"], "RUNNING")
        self.assertEqual(a["_zone"], "us-central1-a")
        self.assertEqual(a["shape"], "e2-small")
        self.assertEqual(a["region"], "us-central1")
        b = next(r for r in rows if r["name"] == "vm-b")
        self.assertIsNone(b["public_ip"])
        self.assertEqual(b["state"], "STOPPED")

    def test_instance_op_paths(self):
        seen = []

        def h(method, path, params, body):
            seen.append(path)
            return {}
        self._patch(h)
        gcp_cloud.instance_op(self.acct, "vm-a", "us-central1-a", "start")
        gcp_cloud.instance_op(self.acct, "vm-a", "us-central1-a", "reboot")
        gcp_cloud.instance_op(self.acct, "vm-a", "us-central1-a", "STOP")
        assert "/zones/us-central1-a/instances/vm-a/start" in seen[0]
        assert seen[1].endswith("/instances/vm-a/reset")
        assert seen[2].endswith("/instances/vm-a/stop")

    def test_change_public_ip_flow(self):
        state = {"phase": 0, "ip": "34.1.2.3"}

        def h(method, path, params, body):
            if method == "GET" and path.endswith("/instances/vm-a"):
                # addAccessConfig 后第二次 GET 起返回新 IP
                return _ins_payload("vm-a", nat_ip=state["ip"])
            if method == "DELETE":
                assert "deleteAccessConfig" in path
                assert params.get("accessConfigName") == "External NAT"
                return {}
            if method == "POST" and "addAccessConfig" in path:
                state["ip"] = "34.9.9.9"   # 新 IP 生效
                assert body["natIP"] == ""
                return {}
            raise AssertionError(f"{method} {path}")

        self._patch(h)
        logs = []
        res = gcp_cloud.change_public_ip(
            logs.append, self.acct, "", "vm-a@us-central1-a")
        self.assertEqual(res["old_ip"], "34.1.2.3")
        self.assertEqual(res["new_ip"], "34.9.9.9")
        joined = "\n".join(str(x) for x in logs)
        self.assertIn("新公网 IP", joined)

    def test_create_instance_happy_path(self):
        ops = {"n": 0}

        def h(method, path, params, body):
            if method == "POST" and path.endswith("/zones/us-central1-a/instances"):
                self.assertEqual(body["name"], "web-1")
                self.assertEqual(body["machineType"],
                                 "zones/us-central1-a/machineTypes/e2-small")
                disk = body["disks"][0]["initializeParams"]
                self.assertEqual(disk["sourceImage"],
                                 "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64")
                nic = body["networkInterfaces"][0]
                self.assertEqual(nic.get("network"), "global/networks/default")
                self.assertEqual(nic["accessConfigs"][0]["type"], "ONE_TO_ONE_NAT")
                sk = [m for m in body["metadata"]["items"] if m["key"] == "ssh-keys"]
                self.assertTrue(sk[0]["value"].startswith("clouddeck:ssh-ed25519"))
                return {"name": "op-1", "status": "RUNNING"}
            if path.endswith("/operations/op-1"):
                ops["n"] += 1
                return {"status": "DONE" if ops["n"] >= 2 else "PENDING",
                        **({"error": {"errors": [{"message": "boom"}]}}
                           if False else {})}
            if method == "GET" and path.endswith("/instances/web-1"):
                return _ins_payload("web-1", nat_ip="35.2.2.2")
            raise AssertionError(f"{method} {path}")

        self._patch(h)
        logs = []
        res = gcp_cloud.create_instance(logs.append, self.acct, {
            "name": "web-1", "zone": "us-central1-a", "machine_type": "e2-small",
            "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            "ssh_key": "ssh-ed25519 AAAAtest me@local"})
        self.assertEqual(res["instance_id"], "12345")
        self.assertEqual(res["public_ip"], "35.2.2.2")

    def test_create_with_subnet_and_custom_boot(self):
        captured = {}

        def h(method, path, params, body):
            if method == "POST":
                captured.update(body)
                return {"name": "op-2", "status": "DONE"}
            if "operations/op-2" in path:
                return {"status": "DONE"}
            if method == "GET" and path.endswith("/instances/db-1"):
                return _ins_payload("db-1", nat_ip="")
            raise AssertionError(f"{method} {path}")

        self._patch(h)
        gcp_cloud.create_instance(lambda m: None, self.acct, {
            "name": "db-1", "zone": "europe-west1-d", "machine_type": "n2-standard-2",
            "image": "projects/debian-cloud/global/images/family/debian-12",
            "subnet": "my-subnet", "ssh_key": "ssh-rsa AAAAx", "boot_gbs": 50,
            "external_ip": False})
        nic = captured["networkInterfaces"][0]
        self.assertEqual(nic.get("subnetwork"), "regions/europe-west1/subnetworks/my-subnet")
        self.assertNotIn("accessConfigs", nic)   # 无外网 IP
        self.assertEqual(captured["disks"][0]["initializeParams"]["diskSizeGb"], "50")

    def test_meta_and_machtypes(self):
        def h(method, path, params, body):
            if path.endswith("/zones"):
                return {"items": [{"name": "us-central1-a"}, {"name": "us-central1-b"}]}
            if path.endswith("/machineTypes"):
                return {"items": [
                    {"name": "e2-small", "guestCpus": 2, "memoryMb": 2048},
                    {"name": "e2-medium", "guestCpus": 2, "memoryMb": 4096}]}
            raise AssertionError(path)

        self._patch(h)
        meta = gcp_cloud.gcp_meta(self.acct)
        self.assertEqual(meta["zones"], ["us-central1-a", "us-central1-b"])
        self.assertGreater(len(meta["images"]), 4)
        mts = gcp_cloud.mach_types(self.acct, "us-central1-a")
        self.assertEqual(mts[0]["cpu"], 2)

    def test_bad_action_rejected(self):
        with self.assertRaises(ProviderError):
            gcp_cloud.instance_op(self.acct, "vm", "z", "destroy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
