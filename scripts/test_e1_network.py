"""Safety checks for isolated network ownership; no networking is changed."""

import ipaddress
import subprocess
import unittest
from unittest.mock import patch

from e1_network import NetworkHarness


class NetworkSafetyTests(unittest.TestCase):
    def setUp(self):
        mkdir = patch("e1_network.Path.mkdir")
        write_json = patch("e1_network._write_json")
        mkdir.start()
        write_json.start()
        self.addCleanup(mkdir.stop)
        self.addCleanup(write_json.stop)
        with patch("e1_network.os.geteuid", return_value=0, create=True):
            self.network = NetworkHarness("mock-output-directory", rate_mbit=2)

    def test_does_not_select_existing_address_or_route(self):
        self.network.token = "0000000000"
        evidence = [
            [{"addr_info": [{"local": "10.203.0.1", "prefixlen": 30}]}],
            [{"dst": "default"}, {"dst": "10.203.1.0/24"}],
        ]
        with patch.object(self.network, "_json", side_effect=evidence):
            self.assertEqual(self.network._unused_subnet(), ipaddress.ip_network("10.203.2.0/30"))

    def test_refuses_overlap_with_broad_private_route(self):
        with patch.object(self.network, "_json", side_effect=[[], [{"dst": "10.0.0.0/8"}]]):
            with self.assertRaisesRegex(RuntimeError, "No unused"):
                self.network._unused_subnet()

    def test_cleanup_deletes_only_verified_owned_names(self):
        self.network._namespace_created = True
        self.network._link_created = True
        success = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.network, "_json", return_value=[{"ifalias": self.network.alias}]), \
                patch.object(self.network, "_run", return_value=success) as command:
            self.network.cleanup()
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            ["ip", "link", "delete", "dev", self.network.gateway_interface],
            ["ip", "netns", "delete", self.network.namespace],
        ])
        self.assertFalse(self.network._namespace_created)
        self.assertFalse(self.network._link_created)

    def test_cleanup_refuses_changed_interface_owner(self):
        self.network._namespace_created = True
        self.network._link_created = True
        with patch.object(self.network, "_json", return_value=[{"ifalias": "someone-else"}]), \
                patch.object(self.network, "_run") as command:
            self.network.cleanup()
        command.assert_not_called()
        self.assertTrue(self.network._namespace_created)
        self.assertTrue(self.network._link_created)

    def test_cleanup_unstarted_harness_does_not_touch_network(self):
        with patch.object(self.network, "_run") as command:
            self.network.cleanup()
        command.assert_not_called()

    def test_invalid_rate_is_rejected(self):
        with patch("e1_network.os.geteuid", return_value=0, create=True):
            with self.assertRaises(ValueError):
                NetworkHarness("mock-output-directory", rate_mbit=-1)


if __name__ == "__main__":
    unittest.main()
