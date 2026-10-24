"""Check controlled TCP comparisons without creating network namespaces."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run_trials import build_plan, load_config, main, parser, HERE, resolve_output


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.base = load_config(HERE / "configs/tcp_cubic.json")

    def args(self, *values):
        return parser().parse_args(list(values))

    def test_full_topology_changes_only_wan_sender_between_algorithms(self):
        cells = build_plan(self.base, self.args("--repeats", "1"))
        self.assertEqual({row["cell"]["algorithm"] for row in cells}, {"reno", "cubic", "bbr"})
        configs = []
        for item in cells:
            config = copy.deepcopy(item["config"])
            self.assertEqual(config["topology"], "T3_EDGE_PASS")
            self.assertEqual(config["tcp_congestion"]["edge"], item["cell"]["algorithm"])
            self.assertEqual(config["tcp_congestion"]["robot"], "cubic")
            config["tcp_congestion"]["edge"] = "normalized"
            config.pop("tcp_trial")
            configs.append(config)
        self.assertEqual(configs[0], configs[1])
        self.assertEqual(configs[1], configs[2])

    def test_all_presets_select_edge_algorithm_with_identical_workload(self):
        from run_trials import validate_config
        configs = []
        for algorithm in ("reno", "cubic", "bbr"):
            config = load_config(HERE / f"configs/tcp_{algorithm}.json")
            validate_config(config)
            self.assertEqual(config["tcp_congestion"]["edge"], algorithm)
            config["tcp_congestion"]["edge"] = "normalized"
            config.pop("description")
            configs.append(config)
        self.assertEqual(configs[0], configs[1])
        self.assertEqual(configs[1], configs[2])

    def test_load_is_rate_multiplier_and_sources_are_actual_clients(self):
        cells = build_plan(self.base, self.args("--sources", "8", "--loads", "1", "4", "--repeats", "1"))
        self.assertEqual(len(cells), 6)
        for item in cells:
            config = item["config"]
            self.assertEqual(len(config["robots"]), 8)
            self.assertEqual([r["site"] for r in config["robots"]].count("A"), 4)
            self.assertEqual(config["traffic"]["odometry"]["rate_hz"], 20 * item["cell"]["load"])
        self.assertEqual(self.base["traffic"]["odometry"]["rate_hz"], 20)

    def test_loss_burst_restores_each_sites_configured_profile(self):
        args = self.args("--duration", "30", "--loss-percent", "1", "--loss-burst", "5", "10", "--repeats", "1")
        for item in build_plan(self.base, args):
            events = item["config"]["impairment_schedule"]
            for site in ("A", "B"):
                selected = [event for event in events if event["site"] == site]
                self.assertEqual([e["at_s"] for e in selected], [5, 10])
                self.assertEqual(selected[0]["profile"]["loss_pct"], 100)
                self.assertEqual(selected[1]["profile"], item["config"]["sites"][site]["forward"])

    def test_direct_topology_selects_robot_socket_and_order_is_repeatable(self):
        base = load_config(HERE.parent / "partner_network/configs/direct.json")
        args = self.args("--repeats", "2")
        first = build_plan(base, args)
        self.assertEqual(first, build_plan(base, args))
        self.assertTrue(all(item["cell"]["wan_sender_role"] == "robot" for item in first))

    def test_invalid_comparisons_are_rejected(self):
        for flags in (("--repeats", "0"), ("--sources", "17"), ("--loads", "1", "1"),
                      ("--delay-ms", "nan"), ("--duration", "5", "--loss-burst", "2", "5")):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                build_plan(self.base, self.args(*flags))

    def test_unavailable_algorithm_fails_before_any_run_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sweep"
            with patch("run_trials.RESULTS_DIR", Path(directory)), \
                 patch("run_trials.doctor", return_value={"tcp_algorithms": ["reno", "cubic"]}), \
                 patch("run_trials.run_experiment") as run:
                with self.assertRaisesRegex(SystemExit, "Unavailable TCP algorithms: bbr"):
                    main(["--output", str(output)])
                run.assert_not_called()
                self.assertFalse(output.exists())

    def test_dry_run_retains_plan_without_running_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan"
            with patch("run_trials.RESULTS_DIR", Path(directory)), \
                 patch("run_trials.doctor", return_value={"tcp_algorithms": ["reno", "cubic"]}), \
                 patch("run_trials.run_experiment") as run:
                main(["--dry-run", "--output", str(output), "--repeats", "1"])
                run.assert_not_called()
            plan = json.loads((output / "plan.json").read_text())
            self.assertEqual(len(plan["runs"]), 3)
            self.assertEqual({run["algorithm"] for run in plan["runs"]}, {"reno", "cubic", "bbr"})
            self.assertEqual(json.loads((output / "environment.json").read_text())["unavailable_algorithms"], ["bbr"])
            for item in plan["runs"]:
                self.assertTrue((output / item["config"]).exists())
            self.assertFalse((output / "sweep_status.json").exists())

    def test_outputs_stay_in_tcp_results_for_default_and_old_cli_syntax(self):
        with tempfile.TemporaryDirectory() as directory, patch("run_trials.RESULTS_DIR", Path(directory)):
            root = Path(directory).resolve()
            self.assertEqual(resolve_output(Path("results/smoke")), root / "smoke")
            self.assertEqual(resolve_output(Path("smoke")), root / "smoke")
            self.assertEqual(resolve_output(root / "smoke"), root / "smoke")
            self.assertEqual(resolve_output(None).parent, root)
            for bad in (Path("../outside"), root.parent / "outside", root, Path("results/../outside")):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    resolve_output(bad)


if __name__ == "__main__":
    unittest.main()
