"""E2B schedule assumptions, exact finite-window accounting, and HTTP checks."""
import copy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from e2_live import occupancy_area, population_identity, resolved_run_config, validate_config, wait_json, read_csv
from e2_source import make_schedule, PATTERNS, wait_until_ns
from validate_e2_live import replay_queue, verify_little


def configuration():
    return {"actual_source_counts": [4], "conditions": [{"id": p, "arrival_pattern": p} for p in PATTERNS],
            "actual_sources": 4, "arrival_pattern": "poisson", "duration_s": 120, "warmup_s": 20,
            "drain_s": 60, "repeats": 5, "queue_capacity": 1000, "request_timeout_s": 3,
            "seed": 261023, "server_processing_delay_ms": 4, "gateway_dispatch_delay_ms": 0,
            "network_mode": "dedicated_veth", "network_rate_mbit": None,
            "bursty_model": {"cycle_s": 1.0, "on_s": .2},
            "traffic": {"command": {"rate_hz": 2, "payload_bytes": 100}, "status": {"rate_hz": 5, "payload_bytes": 100},
                        "odometry": {"rate_hz": 20, "payload_bytes": 250}, "sensor": {"rate_hz": 5, "payload_bytes": 20000}},
            "deadlines_ms": {"command": 100, "status": 200, "odometry": 100, "sensor": 1000}}


def boundary_fixture():
    rows = [{"id": "a", "enqueue_ns": 5, "dequeue_ns": 6, "response_ns": 12},
            {"id": "b", "enqueue_ns": 9, "dequeue_ns": 13, "response_ns": 18},
            {"id": "c", "enqueue_ns": 17, "dequeue_ns": 19, "response_ns": 25}]
    transitions = [(5, "admit", "a", 1, 0), (6, "dequeue", "a", 0, 1), (9, "admit", "b", 1, 1),
                   (12, "complete", "a", 1, 0), (13, "dequeue", "b", 0, 1), (17, "admit", "c", 1, 1),
                   (18, "complete", "b", 1, 0), (19, "dequeue", "c", 0, 1), (25, "complete", "c", 0, 0)]
    events = [{"timestamp_ns": t, "action": a, "id": i, "queue_length": q, "in_flight": b, "system_length": q+b}
              for t, a, i, q, b in transitions]
    return rows, events


class E2Tests(unittest.TestCase):
    def test_only_arrival_pattern_can_vary_between_conditions(self):
        config = configuration()
        validate_config(config)
        for condition in config["conditions"]:
            resolved = resolved_run_config(config, condition, 4, 1, Path("fixture"))
            self.assertEqual(resolved["nominal_offered_rps"], 128)
            self.assertEqual(resolved["server_processing_delay_ms"], 4)
            self.assertEqual(resolved["source_rate_multiplier"], 1)
        config["conditions"][0]["server_processing_delay_ms"] = 5
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_periodic_phase_relationships_and_class_counts(self):
        config = configuration()
        for pattern in ("periodic_staggered", "periodic_synchronized"):
            config["arrival_pattern"] = pattern
            schedule, _ = make_schedule(config)
            for robot in range(4):
                for kind, traffic in config["traffic"].items():
                    stream = [offset for offset, r, k, seq in schedule if (r, k) == (robot, kind)]
                    self.assertEqual(len(stream), 140 * traffic["rate_hz"])
                    self.assertTrue(all(abs((b-a)-1e9/traffic["rate_hz"]) < 2 for a,b in zip(stream,stream[1:])))
            phases = [offset for offset, robot, kind, seq in schedule if seq == 0]
            if pattern == "periodic_synchronized":
                self.assertEqual(phases, [0] * 16)
            else:
                self.assertGreater(len(set(phases)), 8)

    def test_poisson_and_burst_schedules_are_reproducible_and_correct_in_expectation(self):
        config = configuration()
        config.update(warmup_s=0, duration_s=1000)
        for pattern in ("poisson", "bursty_onoff_poisson"):
            config["arrival_pattern"] = pattern
            schedule, metadata = make_schedule(config)
            self.assertEqual((schedule, metadata), make_schedule(config))
            self.assertLess(abs(len(schedule)-128000), 6 * (128000 ** .5))
            for offset, robot, kind, seq in schedule:
                if pattern == "bursty_onoff_poisson":
                    self.assertLess(offset % 1_000_000_000, 200_000_000)
            for robot in range(4):
                for kind, traffic in config["traffic"].items():
                    stream = [offset for offset, r, k, seq in schedule if (r, k) == (robot, kind)]
                    expected = 1000 * traffic["rate_hz"]
                    self.assertLess(abs(len(stream)-expected), 6 * expected ** .5)
                    if pattern == "poisson":
                        intervals = [(b-a)/1e9 for a,b in zip(stream, stream[1:])]
                        mean = sum(intervals)/len(intervals)
                        cv2 = sum((x-mean)**2 for x in intervals)/len(intervals)/mean**2
                        self.assertTrue(.75 < cv2 < 1.25)

    def test_burst_measurement_requires_whole_cycles(self):
        config = configuration()
        config["warmup_s"] = .5
        with self.assertRaisesRegex(ValueError, "whole burst cycles"):
            validate_config(config)

    def test_quiet_tail_remains_inside_observation_horizon(self):
        config = configuration()
        config.update(arrival_pattern="bursty_onoff_poisson", warmup_s=5, duration_s=20)
        schedule, _ = make_schedule(config)
        last = schedule[-1][0]
        horizon = 25_000_000_000
        self.assertLess(last, horizon - 800_000_000)
        now = [last]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            # Simulate an early wake on the first sleep, then reach the horizon.
            now[0] += int(seconds * 1e9) // (2 if len(sleeps) == 1 else 1)
        wait_until_ns(horizon, clock=lambda: now[0], sleeper=sleep)
        self.assertEqual(now[0], horizon)
        self.assertGreaterEqual(len(sleeps), 2)
        self.assertGreater(sum(sleeps), .8)

    def test_finite_window_little_identity_keeps_carry_in_and_tail(self):
        rows, events = boundary_fixture()
        q = population_identity(rows, 10, 20, "dequeue_ns")
        system = population_identity(rows, 10, 20, "response_ns")
        self.assertEqual((q["overlap_area_ns"], q["carry_in_area_ns"], q["carry_out_area_ns"]), (5,3,0))
        self.assertEqual((system["overlap_area_ns"], system["carry_in_area_ns"], system["carry_out_area_ns"]), (13,10,5))
        self.assertEqual(system["identity_residual_ns"], 0)
        self.assertNotEqual(system["uncorrected_lambda_W"], system["corrected_lambda_W"])
        self.assertEqual(occupancy_area(events,10,20,"system_length")[0],13)
        replay = replay_queue(rows,events,10,20,1000)
        self.assertEqual((replay["queue_area_ns"],replay["busy_area_ns"],replay["system_area_ns"]),(5,8,13))
        self.assertEqual((replay["initial_population"], replay["final_population"]),(2,1))
        verify_little(rows,10,20,"dequeue_ns",q,5)
        verify_little(rows,10,20,"response_ns",system,13)

    def test_independent_replay_detects_timestamp_offset_and_nonfifo_service(self):
        rows, events = boundary_fixture()
        bad = copy.deepcopy(events)
        bad[2]["timestamp_ns"] += 1
        with self.assertRaisesRegex(AssertionError, "Event timestamps"):
            replay_queue(rows,bad,10,20,1000)
        bad = copy.deepcopy(events)
        bad[2]["queue_length"] = 2
        with self.assertRaisesRegex(AssertionError, "queue trace"):
            replay_queue(rows,bad,10,20,1000)

    def test_live_http_hold_and_persistent_single_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = {"server_bind":"127.0.0.1", "server_port":0, "server_processing_delay_ms":4, "request_timeout_s":2}
            (folder/"config.json").write_text(json.dumps(config))
            process = subprocess.Popen([sys.executable,"-B",str(Path(__file__).with_name("e2_live.py")),"--server",
                                       "--config",str(folder/"config.json"),"--output",str(folder)],
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            connection = None
            try:
                ready = wait_json(folder/"server_ready.json",process)
                connection = http.client.HTTPConnection("127.0.0.1",ready["port"],timeout=2)
                ids = set()
                for seq in range(3):
                    body = json.dumps({"id":str(seq),"robot_id":0,"message_type":"command","payload":"x"*100})
                    connection.request("POST","/robots/0/command",body,{"Content-Type":"application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status,200)
                    ack = json.loads(response.read())
                    self.assertGreaterEqual(ack["server_processed_ns"]-ack["server_processing_start_ns"],4_000_000)
                    ids.add(ack["connection_id"])
                self.assertEqual(ids,{1})
            finally:
                if connection:
                    connection.close()
                (folder/"server.stop").touch()
                try:
                    stdout,stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout,stderr = process.communicate()
            self.assertEqual(process.returncode,0,stderr)
            self.assertEqual(len(read_csv(folder/"server.csv")),3)


if __name__ == "__main__":
    unittest.main()
