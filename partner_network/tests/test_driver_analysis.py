import json
from pathlib import Path
import tempfile
import unittest
import struct
from unittest.mock import patch

from lab.analyze import replay_queue, outcome_metrics, pcap_record_count
from lab.config import load_config, validate_config
from lab.runner import active_names, resolve_endpoints, owned_alive


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root / "configs/base.json")

    def test_presets_are_valid_and_change_only_declared_dimensions(self):
        for path in (self.root / "configs").glob("*.json"):
            validate_config(load_config(path))

    def test_config_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.json"
            path.write_text(json.dumps({"extends":"a.json"}))
            with self.assertRaisesRegex(ValueError, "cycle"):
                load_config(path)

    def test_independent_client_endpoints_match_topology(self):
        nodes = {name: {"ip": f"10.0.0.{index+1}"} for index, name in enumerate(active_names(self.config))}
        full = resolve_endpoints(self.config, nodes)
        self.assertEqual(full["robot1"]["target_url"], "http://" + nodes["edgeA"]["ip"] + ":8080")
        direct = dict(self.config, topology="T1_DISTRIBUTED", workers=1)
        self.assertEqual(active_names(direct), ["worker1", "robot1", "robot2", "robot3", "robot4"])
        mapped = resolve_endpoints(direct, nodes)
        self.assertEqual(len({mapped[r["id"]]["target_url"] for r in self.config["robots"]}), 1)

    def test_queue_integration_keeps_carry_in_and_drain(self):
        events = [
            {"event":"initial","id":None,"time_ns":0,"queue_len":0,"in_service":0},
            {"event":"admit","id":"a","time_ns":1,"queue_len":1,"in_service":0},
            {"event":"service_start","id":"a","time_ns":2,"queue_len":0,"in_service":1},
            {"event":"admit","id":"b","time_ns":3,"queue_len":1,"in_service":1},
            {"event":"service_end","id":"a","time_ns":8,"queue_len":1,"in_service":0},
            {"event":"service_start","id":"b","time_ns":9,"queue_len":0,"in_service":1},
            {"event":"service_end","id":"b","time_ns":12,"queue_len":0,"in_service":0}]
        metrics, _ = replay_queue(events, 4, 10, 2, 1)
        self.assertEqual(metrics["mean_Lq"], 5/6)
        self.assertEqual(metrics["mean_L"], 10/6)
        changed = [dict(e) for e in events]
        changed[4]["queue_len"] = 0
        with self.assertRaises(ValueError):
            replay_queue(changed, 4, 10, 2, 1)

    def test_deadline_denominator_retains_drops_and_ros_missing(self):
        source = [{"id":s,"source_ns":2,"scheduled_ns":1,"payload_bytes":100} for s in ("a","b","c")]
        terminal = {"a":dict(source[0],outcome="success",response_ns=3),"b":dict(source[1],outcome="queue_drop")}
        result = outcome_metrics(source, terminal, 0, 10, 100)
        self.assertEqual(result["successful"], 1)
        self.assertEqual(result["deadline_miss_fraction"], 2/3)
        self.assertEqual(result["outcomes"]["ros_missing"], 1)

    def test_cleanup_rejects_another_boot_before_pid_inspection(self):
        with patch("lab.runner.Path.exists", return_value=True), patch("lab.runner.Path.read_text", return_value="new-boot"), patch("lab.runner.proc_start") as inspect_pid:
            self.assertFalse(owned_alive({"boot_id":"old-boot", "pid":123, "start_ticks":"10"}))
            inspect_pid.assert_not_called()

    def test_pcap_framing_rejects_truncated_packets(self):
        header = struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
        packet = struct.pack("<IIII", 1, 0, 4, 4) + b"abcd"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pcap"
            path.write_bytes(header + packet)
            self.assertEqual(pcap_record_count(path), 1)
            path.write_bytes(header + packet[:-1])
            with self.assertRaisesRegex(ValueError, "Truncated"):
                pcap_record_count(path)


if __name__ == "__main__":
    unittest.main()
