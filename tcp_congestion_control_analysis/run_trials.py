"""Repeatable TCP comparisons using the shared partner_network implementation."""
import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import signal
import sys
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RESULTS_DIR = HERE / "results"
sys.path.insert(0, str(REPO / "partner_network"))
from lab.config import load_config, validate_config
from lab.runner import doctor, run_experiment


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Use a finite number greater than zero")
    return result


def loss_value(value):
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("Loss must be between 0 and 100 percent")
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, default=HERE / "configs/tcp_cubic.json")
    result.add_argument("--output", type=Path, help="Fresh directory inside tcp_congestion_control_analysis/results (name or results/name)")
    result.add_argument("--tcp", nargs="+", choices=("reno", "cubic", "bbr"), default=["reno", "cubic", "bbr"],
                        help="Algorithms to compare (default: reno cubic bbr)")
    result.add_argument("--loads", nargs="+", type=positive, default=[1.0], help="Per-source rate multipliers, not robot counts")
    result.add_argument("--loss-percent", nargs="+", type=loss_value, default=[0.0], help="Forward packet loss at both site uplinks")
    result.add_argument("--sources", "--robots", dest="sources", type=int, help="Actual clients, 1--16; default keeps the base config")
    result.add_argument("--repeats", type=int, default=3)
    result.add_argument("--duration", type=positive)
    result.add_argument("--warmup", type=float)
    result.add_argument("--drain", type=positive)
    result.add_argument("--delay-ms", type=float, help="Added delay per direction at each site")
    result.add_argument("--bandwidth-mbit", type=positive, help="Forward rate per site")
    result.add_argument("--seed", type=int, default=610610)
    result.add_argument("--loss-burst", nargs=2, type=float, metavar=("START", "END"), help="Measurement seconds; restore baseline loss at END")
    result.add_argument("--burst-loss-percent", type=loss_value, default=100.0)
    result.add_argument("--dry-run", action="store_true", help="Write the plan/configs without starting processes or changing networking")
    return result


def build_plan(base, args):
    if args.repeats < 1 or (args.sources is not None and not 1 <= args.sources <= 16):
        raise ValueError("Use at least one repeat and 1--16 actual sources")
    for values in (args.tcp, args.loads, args.loss_percent):
        if len(values) != len(set(values)):
            raise ValueError("Duplicate algorithms, loads or loss conditions")
    fixed = copy.deepcopy(base)
    for option, field in (("duration", "duration_s"), ("warmup", "warmup_s"), ("drain", "drain_s")):
        value = getattr(args, option)
        if value is not None:
            fixed[field] = value
    if args.sources is not None:
        fixed["robots"] = [{"id": f"robot{i+1}", "site": "A" if i < (args.sources + 1)//2 else "B"}
                           for i in range(args.sources)]
    for site in ("A", "B"):
        if args.delay_ms is not None:
            for direction in ("forward", "reverse"):
                fixed["sites"][site][direction]["delay_ms"] = args.delay_ms
        if args.bandwidth_mbit is not None:
            fixed["sites"][site]["forward"]["rate_mbit"] = args.bandwidth_mbit
    if args.loss_burst is not None:
        start, end = args.loss_burst
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end < fixed["duration_s"]):
            raise ValueError("Loss burst must satisfy 0 <= START < END < duration")
        if fixed.get("impairment_schedule"):
            raise ValueError("Loss burst cannot replace an existing impairment schedule")
    role = "edge" if fixed["topology"] == "T3_EDGE_PASS" else "robot"
    cells = []
    for repeat in range(1, args.repeats + 1):
        for load in args.loads:
            for loss in args.loss_percent:
                for algorithm in args.tcp:
                    config = copy.deepcopy(fixed)
                    config["seed"] = args.seed + repeat
                    config["tcp_congestion"][role] = algorithm
                    for traffic in config["traffic"].values():
                        traffic["rate_hz"] *= load
                    for site in ("A", "B"):
                        config["sites"][site]["forward"]["loss_pct"] = loss
                    if args.loss_burst is not None:
                        events = []
                        for site in ("A", "B"):
                            restore = dict(config["sites"][site]["forward"])
                            events.extend([
                                {"at_s": start, "site": site, "direction": "forward",
                                 "profile": dict(restore, loss_pct=args.burst_loss_percent)},
                                {"at_s": end, "site": site, "direction": "forward", "profile": restore},
                            ])
                        config["impairment_schedule"] = sorted(events, key=lambda event: event["at_s"])
                    identity = {"algorithm": algorithm, "wan_sender_role": role, "load": load,
                                "loss_percent": loss, "repeat": repeat}
                    config["tcp_trial"] = identity
                    validate_config(config)
                    cells.append({"cell": identity, "config": config})
    random.Random(args.seed).shuffle(cells)
    return cells


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def resolve_output(output):
    """Keep every TCP sweep under this section, even with the old results/name syntax."""
    root = RESULTS_DIR.resolve()
    if output is None:
        output = Path("tcp_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:6])
    output = Path(output)
    if output.is_absolute():
        target = output.resolve()
    elif output.resolve().is_relative_to(root):
        target = output.resolve()
    elif output.parts and output.parts[0] == "results":
        target = root.joinpath(*output.parts[1:]).resolve()
    else:
        target = (root / output).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError(f"TCP results must be in a new subdirectory of {root}")
    return target


def write_comparison(output, rows):
    if not rows:
        return
    with (output / "comparison.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def comparison_row(cell, run_directory, summary):
    return dict(cell, run=run_directory.name, generated=summary["generated"],
                successful=summary["successful"], completed_rps=summary["completed_rps"],
                payload_goodput_mbit_s=sum(r["payload_goodput_mbit_s"] for r in summary["per_robot"].values()),
                latency_p95_ms=summary["source_to_response_ms"]["p95"],
                latency_p99_ms=summary["source_to_response_ms"]["p99"],
                outcomes=json.dumps(summary["outcomes"], sort_keys=True),
                accounting_passed=summary["accounting_passed"])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cells = build_plan(load_config(args.config), args)
        output = resolve_output(args.output)
    except (ValueError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc
    environment = doctor(cells[0]["config"])
    available = set(environment["tcp_algorithms"])
    unavailable = sorted(set(args.tcp) - available)
    if not args.dry_run:
        if unavailable:
            raise SystemExit(f"Unavailable TCP algorithms: {', '.join(unavailable)}. Kernel offers: {', '.join(sorted(available))}. No runs started.")
        if not environment["passed"]:
            raise SystemExit("Prerequisites failed: " + json.dumps(environment))
        if not environment["root"]:
            raise SystemExit("Run with sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh ...")
    output.mkdir(parents=True, exist_ok=False)
    config_dir = output / "configs"
    config_dir.mkdir()
    plan = []
    for index, item in enumerate(cells, 1):
        name = f"run_{index:03d}_{item['cell']['algorithm']}"
        config_path = config_dir / (name + ".json")
        write_json(config_path, item["config"])
        plan.append(dict(item["cell"], run=name, config=str(config_path.relative_to(output))))
    write_json(output / "plan.json", {"seed": args.seed, "dry_run": args.dry_run, "runs": plan})
    write_json(output / "environment.json", dict(environment, requested_algorithms=args.tcp, unavailable_algorithms=unavailable))
    (output / "trial_runner_snapshot.py").write_bytes(Path(__file__).read_bytes())
    write_json(output / "trial_provenance.json", {
        "base_config": str(args.config.resolve()),
        "base_config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "note": "Fully resolved per-run configs retain inherited settings; network-kit code snapshots are saved in each live run.",
    })
    print(f"{'PLAN' if args.dry_run else 'SWEEP'} {len(plan)} runs; output={output}", flush=True)
    if args.dry_run:
        print(f"No network changes. Unavailable algorithms: {unavailable}")
        return 0
    def terminate(_number, _frame):
        raise KeyboardInterrupt("Termination requested")
    signal.signal(signal.SIGTERM, terminate)
    rows = []
    for index, (item, planned) in enumerate(zip(cells, plan), 1):
        run_directory = output / planned["run"]
        print(f"START {index}/{len(plan)} {item['cell']}", flush=True)
        try:
            summary = run_experiment(item["config"], run_directory)
            rows.append(comparison_row(item["cell"], run_directory, summary))
            write_comparison(output, rows)
        except BaseException as exc:
            write_json(output / "sweep_status.json", {"passed": False, "completed_runs": len(rows),
                       "planned_runs": len(plan), "failed_run": planned["run"], "reason": repr(exc)})
            raise
    write_json(output / "sweep_status.json", {"passed": True, "completed_runs": len(rows), "planned_runs": len(plan)})
    try:
        from plot_results import generate_report
        report = generate_report(output)
        write_json(output / "plot_status.json", {"passed": True, "directory": str(report)})
        print(f"GRAPHS {report}", flush=True)
    except Exception as exc:
        # Saved measurements remain valid even if optional plotting dependencies fail.
        write_json(output / "plot_status.json", {"passed": False, "reason": repr(exc)})
        print(f"Graphs could not be generated: {exc}. Re-run plot_results.py after resolving this.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
