"""Fast ownership/plan tests. These never create namespaces or change networking."""

import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.network import LabNetwork, select_subnets, validate_config, validate_profile, verify_installed_profile


def config():
    return {"robots": [{"id": f"robot{i}", "site": "A" if i <= 2 else "B"} for i in range(1, 5)],
            "workers": 2, "capture": False}


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = LabNetwork(Path(self.temp.name), config())
        self.lab._build_plan(["10.220.70.0/24", "10.220.71.0/24", "10.220.72.0/24"])

    def test_plan_has_distinct_namespaces_ips_and_safe_interface_lengths(self):
        self.assertEqual(len(self.lab.nodes), 11)
        self.assertEqual(len({node["namespace"] for node in self.lab.nodes.values()}), 11)
        self.assertEqual(self.lab.nodes["robot1"]["ip"], "10.220.70.20")
        self.assertEqual(self.lab.nodes["robot3"]["ip"], "10.220.71.20")
        self.assertEqual(self.lab.nodes["worker2"]["ip"], "10.220.72.21")
        ips, names = [], list(self.lab.bridges.values())
        for node in self.lab.nodes.values():
            for interface in node["interfaces"].values():
                ips.append(interface["ip"])
                names.extend((interface["host_veth"], interface["temporary_peer"]))
        self.assertEqual(len(ips), len(set(ips)))
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(len(name) <= 15 for name in names))

    def test_largest_supported_plan_has_no_address_or_name_collisions(self):
        expanded = config()
        expanded["robots"] = [{"id": f"robot{i}", "site": "A"} for i in range(1, 17)]
        expanded["workers"] = 8
        self.lab.config = validate_config(expanded)
        self.lab.nodes = {}
        self.lab._build_plan(["10.220.70.0/24", "10.220.71.0/24", "10.220.72.0/24"])
        ips = [interface["ip"] for node in self.lab.nodes.values() for interface in node["interfaces"].values()]
        self.assertEqual(len(ips), len(set(ips)))
        self.assertEqual(len(self.lab.nodes), 29)

    def test_explicit_routes_reach_all_three_subnets_without_default_route(self):
        for name, node in self.lab.nodes.items():
            routes = self.lab._routes_for(name)
            connected = {interface["subnet"] for interface in node["interfaces"].values()}
            self.assertEqual(connected | {route[0] for route in routes}, set(self.lab.subnets.values()))
            for route in routes:
                self.assertNotEqual(route[0], "default")
                self.assertTrue(any(ipaddress.ip_address(route[2]) in ipaddress.ip_network(subnet) for subnet in connected))

    def test_command_is_literal_argv_and_unknown_nodes_fail(self):
        args = ["python3", "script with spaces.py", "$(not-a-shell)"]
        command = self.lab.command("robot1", args)
        self.assertEqual(command[-3:], args)
        self.assertEqual(command[:4], ["ip", "netns", "exec", self.lab.nodes["robot1"]["namespace"]])
        with self.assertRaises(KeyError):
            self.lab.command("host", ["true"])
        with self.assertRaises(ValueError):
            self.lab.command("robot1", "echo unsafe")

    def test_allocator_excludes_addresses_broad_routes_and_wraps(self):
        addresses = [{"addr_info": [{"family": "inet", "local": "10.220.255.9", "prefixlen": 24}]}]
        routes = [{"dst": "default"}, {"dst": "10.220.0.0/23"}, {"dst": "10.220.3.0/24"}]
        self.assertEqual(select_subnets(addresses, routes, 255), ["10.220.2.0/24", "10.220.4.0/24", "10.220.5.0/24"])
        with self.assertRaises(RuntimeError):
            select_subnets([], [{"dst": "10.0.0.0/8"}])

    def test_profile_rejects_nonfinite_invalid_and_misspelled_values(self):
        for profile in ({"delay_ms": -1}, {"delay_ms": float("nan")}, {"loss_pct": 101},
                        {"rate_mbit": 0}, {"rate_mbit": "2"}, {"rate_mbit": True},
                        {"limit_packets": 0}, {"limit_packets": 1.2}, {"delay": 5}):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                validate_profile(profile)
        valid = validate_profile({"delay_ms": 25, "loss_pct": 1, "rate_mbit": 2})
        self.assertEqual(valid["limit_packets"], 1000)

    def test_installed_netem_settings_are_checked_in_the_correct_units(self):
        requested = validate_profile({"delay_ms": 20, "loss_pct": 1, "rate_mbit": 2})
        qdiscs = [{"kind": "netem", "handle": "1:", "root": True,
                   "options": {"limit": 1000, "delay": {"delay": 0.020},
                               "loss-random": {"loss": 0.01}, "rate": {"rate": 250000}}}]
        self.assertTrue(verify_installed_profile(requested, qdiscs)["matches_requested"])
        qdiscs[0]["options"]["rate"]["rate"] = 2000000  # Would be 16 Mbit/s, not 2.
        with self.assertRaisesRegex(RuntimeError, "differ from the request"):
            verify_installed_profile(requested, qdiscs)
        with self.assertRaisesRegex(RuntimeError, "remains"):
            verify_installed_profile(validate_profile({}), qdiscs)

    def test_config_validation_rejects_duplicate_robots_or_unknown_sites(self):
        for change in ({"robots": [{"id": "robot1", "site": "A"}] * 2}, {"workers": 0},
                       {"workers": True}, {"sites": {"C": {}}}, {"capture": "yes"},
                       {"robots": [{"id": "routerA", "site": "A"}]},
                       {"robots": [{"id": "robot1", "site": "C"}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config({**config(), **change})

    def test_existing_journal_is_never_overwritten_or_adopted(self):
        self.lab._save()
        original = self.lab.journal_path.read_bytes()
        with self.assertRaises(FileExistsError):
            LabNetwork(Path(self.temp.name), config())
        self.assertEqual(original, self.lab.journal_path.read_bytes())
        loaded = LabNetwork.load(Path(self.temp.name))
        self.assertEqual(loaded.nodes, self.lab.nodes)
        self.assertEqual(loaded.token, self.lab.token)

    def test_missing_ethtool_is_explicit_without_claiming_features_disabled(self):
        with patch("lab.network.shutil.which", return_value=None), patch.object(self.lab, "_run") as run:
            self.lab._initialize_offloads()
            self.lab._configure_veth_offloads("robot1", "owned_host", "eth0")
        run.assert_not_called()
        self.assertEqual(self.lab.offloads["status"], "unavailable")
        self.assertFalse(self.lab.offloads["ethtool_available"])
        self.assertEqual(len(self.lab.offloads["devices"]), 2)
        self.assertTrue(all(row["set"] is None and row["readback"] is None for row in self.lab.offloads["devices"]))
        self.assertEqual(self.lab.topology()["offloads"]["status"], "unavailable")
        loaded = LabNetwork.load(Path(self.temp.name))
        self.assertEqual(loaded.offloads, self.lab.offloads)

    def test_offload_readback_verifies_only_requested_features_on_each_end(self):
        features = ("Features for eth0:\n"
                    "tx-checksumming: on\n"
                    "tcp-segmentation-offload: off\n"
                    "generic-segmentation-offload: off\n"
                    "generic-receive-offload: off [fixed]\n")
        commands = []

        def execute(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, features if "-k" in argv else "", "")

        with patch("lab.network.shutil.which", return_value="/usr/sbin/ethtool"), patch.object(self.lab, "_run", side_effect=execute):
            self.lab._initialize_offloads()
            self.lab._configure_veth_offloads("robot1", "owned_host", "eth0")
        self.assertEqual(self.lab.offloads["status"], "verified")
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[0][:3], ["ethtool", "-K", "owned_host"])
        self.assertEqual(commands[2][:4], ["ip", "netns", "exec", self.lab.nodes["robot1"]["namespace"]])
        self.assertTrue(all(row["readback"]["stdout"] == features for row in self.lab.offloads["devices"]))
        self.assertNotIn("tx-checksumming", self.lab.offloads["requested_disabled_features"])

    def test_successful_offload_command_without_readback_is_unverified(self):
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1 if "-k" in argv else 0, "", "query unsupported" if "-k" in argv else "")

        with patch("lab.network.shutil.which", return_value="/usr/sbin/ethtool"), patch.object(self.lab, "_run", side_effect=execute):
            self.lab._initialize_offloads()
            self.lab._configure_veth_offloads("robot1", "owned_host", "eth0")
        self.assertEqual(self.lab.offloads["status"], "disabled_unverified")
        self.assertTrue(all(row["readback"]["returncode"] == 1 for row in self.lab.offloads["devices"]))

    def test_offload_readback_enabled_feature_is_not_reported_as_disabled(self):
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "tcp-segmentation-offload: on\n" if "-k" in argv else "", "")

        with patch("lab.network.shutil.which", return_value="/usr/sbin/ethtool"), patch.object(self.lab, "_run", side_effect=execute):
            self.lab._initialize_offloads()
            self.lab._configure_veth_offloads("robot1", "owned_host", "eth0")
        self.assertEqual(self.lab.offloads["status"], "not_fully_disabled")

    def test_old_journal_retains_unknown_offload_status(self):
        self.lab._save()
        data = json.loads(self.lab.journal_path.read_text())
        del data["offloads"]
        self.lab.journal_path.write_text(json.dumps(data))
        loaded = LabNetwork.load(Path(self.temp.name))
        self.assertEqual(loaded.offloads["status"], "not_recorded")

    def test_load_refuses_journal_naming_host_resource(self):
        self.lab.resources = [{"kind": "link", "name": "eth0", "created": True, "removed": False}]
        self.lab._save()
        with self.assertRaises(ValueError):
            LabNetwork.load(Path(self.temp.name))

    def test_namespace_identity_and_marker_are_both_required(self):
        record = {"name": "owned", "identity": {"device": 4, "inode": 42}, "marked": True}
        with patch.object(self.lab, "_namespace_inode", return_value={"device": 4, "inode": 43}):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                self.lab._verify_namespace(record)
        with patch.object(self.lab, "_namespace_inode", return_value=record["identity"]), \
                patch.object(self.lab, "_json", return_value=[{"ifalias": "somebody-else"}]):
            with self.assertRaisesRegex(RuntimeError, "marker changed"):
                self.lab._verify_namespace(record)

    def test_cleanup_with_live_application_refuses_all_deletion(self):
        namespace = self.lab.nodes["robot1"]["namespace"]
        self.lab.resources = [{"kind": "namespace", "name": namespace, "created": True, "removed": False}]
        called = []

        def run(argv, **kwargs):
            called.append(argv)
            return subprocess.CompletedProcess(argv, 0, "12345\n", "")

        with patch.object(self.lab, "_require_root"), patch.object(self.lab, "_verify_namespace", return_value=True), \
                patch.object(self.lab, "_run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "cleanup refused"):
                self.lab.cleanup()
        self.assertFalse(any("delete" in args for args in called))
        result = json.loads((self.lab.folder / "cleanup.json").read_text())
        self.assertEqual(result["refusals"][0]["pids"], [12345])
        self.assertFalse(result["success"])

    def test_cleanup_with_reused_interface_refuses_all_deletion(self):
        self.lab.resources = [{"kind": "link", "name": "powned", "ifindex": 42,
                               "created": True, "removed": False, "marked": True}]
        with patch.object(self.lab, "_require_root"), \
                patch.object(self.lab, "_host_link", return_value={"ifindex": 43, "ifalias": self.lab.alias}), \
                patch.object(self.lab, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "cleanup refused"):
                self.lab.cleanup()
        run.assert_not_called()

    def test_cleanup_does_not_detach_foreign_bridge_members(self):
        bridge = self.lab.bridges["A"]
        self.lab.resources = [{"kind": "link", "name": bridge, "ifindex": 42,
                               "created": True, "removed": False, "marked": True}]
        with patch.object(self.lab, "_require_root"), patch.object(self.lab, "_verify_link", return_value=True), \
                patch.object(self.lab, "_json", return_value=[{"ifname": "foreign0", "ifalias": "other"}]), \
                patch.object(self.lab, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "cleanup refused"):
                self.lab.cleanup()
        run.assert_not_called()

    def test_forward_and_reverse_shape_only_the_corresponding_owned_router_device(self):
        namespace = self.lab.nodes["routerA"]["namespace"]
        self.lab.resources = [{"kind": "namespace", "name": namespace, "created": True,
                               "removed": False, "marked": True}]
        commands = []

        def read(argv):
            if "link" in argv:
                return [{"ifalias": self.lab.alias}]
            if "-s" in argv:
                options = {"limit": 1000}
                if "wan0" in argv:
                    options["delay"] = {"delay": 0.02}
                else:
                    options["loss-random"] = {"loss": 0.01}
                    options["rate"] = {"rate": 250000}
                return [{"kind": "netem", "handle": "1:", "root": True, "options": options}]
            return [{"kind": "noqueue", "handle": "0:", "root": True}]

        with patch.object(self.lab, "_require_root"), patch.object(self.lab, "_verify_namespace", return_value=True), \
                patch.object(self.lab, "_json", side_effect=read), \
                patch.object(self.lab, "_run", side_effect=lambda argv: commands.append(argv)):
            self.lab.apply_profile("A", {"delay_ms": 20}, "forward")
            self.lab.apply_profile("A", {"loss_pct": 1, "rate_mbit": 2}, "reverse")
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(argv[:4] == ["ip", "netns", "exec", namespace] for argv in commands))
        self.assertIn("wan0", commands[0])
        self.assertIn("lan0", commands[1])
        self.assertIn("20ms", commands[0])
        self.assertIn("2mbit", commands[1])

    def test_profile_refuses_to_overwrite_unknown_qdisc(self):
        namespace = self.lab.nodes["routerA"]["namespace"]
        self.lab.resources = [{"kind": "namespace", "name": namespace, "created": True,
                               "removed": False, "marked": True}]
        with patch.object(self.lab, "_require_root"), patch.object(self.lab, "_verify_namespace", return_value=True), \
                patch.object(self.lab, "_json", side_effect=[[{"ifalias": self.lab.alias}],
                    [{"kind": "tbf", "handle": "2:", "root": True}]]), \
                patch.object(self.lab, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "unowned root qdisc"):
                self.lab.apply_profile("A", {"delay_ms": 20})
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
