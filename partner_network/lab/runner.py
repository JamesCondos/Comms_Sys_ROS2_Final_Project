"""Own a complete, isolated multi-site experiment and preserve its evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
import zipfile

from .config import load_config, validate_config

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def proc_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned_alive(record):
    boot_file = Path("/proc/sys/kernel/random/boot_id")
    if not boot_file.exists() or record.get("boot_id") != boot_file.read_text().strip():
        return False
    if not record.get("start_ticks") or proc_start(record["pid"]) != record["start_ticks"]:
        return False
    try:
        args = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b"\0")
        return b"lab.components" in args and str(record["config"]).encode() in args
    except (FileNotFoundError, ProcessLookupError):
        return False


def doctor(config=None):
    checks = {"linux": sys.platform.startswith("linux"),
              "python_version": platform.python_version(),
              "ros_rclpy": importlib.util.find_spec("rclpy") is not None,
              "ros_std_msgs": importlib.util.find_spec("std_msgs") is not None,
              "commands": {name: shutil.which(name) for name in ("ip", "tc", "ping", "tcpdump")}}
    algorithms = Path("/proc/sys/net/ipv4/tcp_available_congestion_control")
    checks["tcp_algorithms"] = algorithms.read_text().split() if algorithms.exists() else []
    checks["root"] = hasattr(os, "geteuid") and os.geteuid() == 0
    checks["passed"] = checks["linux"] and checks["ros_rclpy"] and checks["ros_std_msgs"] and all(checks["commands"].values())
    if config:
        requested = {v for v in config["tcp_congestion"].values() if v and v != "system"}
        checks["unsupported_algorithms"] = sorted(requested - set(checks["tcp_algorithms"]))
        checks["passed"] = checks["passed"] and not checks["unsupported_algorithms"]
    return checks


def active_names(config):
    names = [f"worker{i+1}" for i in range(config["workers"])]
    if config["topology"] in ("T2_CENTRAL_POOL", "T3_EDGE_PASS"):
        names.append("proxy")
    if config["topology"] == "T3_EDGE_PASS":
        names += ["edgeA", "edgeB"]
    return names + [r["id"] for r in config["robots"]]


def resolve_endpoints(config, nodes):
    nodes = {key: dict(value) for key, value in nodes.items()}
    url = lambda name: f"http://{nodes[name]['ip']}:8080"
    nodes["proxy"]["backends"] = [url(f"worker{i+1}") for i in range(config["workers"])]
    for site in ("A", "B"):
        nodes["edge" + site]["backends"] = [url("proxy")]
    for robot in config["robots"]:
        target = {"T1_DISTRIBUTED": "worker1", "T2_CENTRAL_POOL": "proxy", "T3_EDGE_PASS": "edge" + robot["site"]}[config["topology"]]
        nodes[robot["id"]]["target_url"] = url(target)
    return nodes


def snapshot_code(folder):
    hashes = {}
    with zipfile.ZipFile(folder / "code_snapshot.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for pattern in ("lab/*.py", "tests/*.py", "configs/*.json", "*.sh"):
            for path in sorted(ROOT.glob(pattern)):
                relative = path.relative_to(ROOT).as_posix()
                data = path.read_bytes()
                archive.writestr(relative, data)
                hashes[relative] = hashlib.sha256(data).hexdigest()
    save(folder / "code_sha256.json", hashes)


def stop_processes(records, processes=None):
    results = []
    for record in reversed(records):
        if owned_alive(record):
            try:
                os.kill(record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and any(owned_alive(r) for r in records):
        time.sleep(.05)
    for record in records:
        forced = owned_alive(record)
        if forced:
            try:
                os.kill(record["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        process = (processes or {}).get(record["name"])
        if process:
            process.wait(timeout=5)
        results.append({"name": record["name"], "pid": record["pid"], "forced_kill": forced,
                        "exit_code": process.returncode if process else None})
    return results


def cpu_sample(records):
    row = {"time_ns": time.monotonic_ns(), "components": {}}
    for record in records:
        try:
            fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()
            row["components"][record["name"]] = {"cpu_s": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                                                        "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE")}
        except (FileNotFoundError, ProcessLookupError):
            pass
    return row


def run_experiment(config, output):
    from .network import LabNetwork
    validate_config(config)
    environment = doctor(config)
    if not environment["passed"]:
        raise RuntimeError("Prerequisites failed: " + json.dumps(environment))
    if not environment["root"]:
        raise RuntimeError("Network namespaces require root: sudo bash run.sh run ...")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    config = dict(config, run_id=run_id, start_file=str(output / "start.json"),
                  launched_utc=datetime.now(timezone.utc).isoformat(), operator_enabled=False,
                  start_timeout_s=35 * len(active_names(config)) + 60)
    save(output / "input_config.json", config)
    save(output / "environment.json", dict(environment, platform=platform.platform(), python=sys.version,
         cpu_count=os.cpu_count(), cpu_affinity=sorted(os.sched_getaffinity(0)),
         shared_host=True, imposed_cpu_quotas=False))
    snapshot_code(output)
    network = LabNetwork(output, config)
    records, processes, streams, readiness, cpu, events = [], {}, [], {}, [], []
    failure = None
    try:
        network.setup()
        config["nodes"] = resolve_endpoints(config, network.nodes)
        config["active_components"] = active_names(config)
        config_path = output / "resolved.json"
        save(config_path, config)
        for name in config["active_components"]:
            node = config["nodes"][name]
            folder = output / "logs" / name
            folder.mkdir(parents=True)
            stream = (folder / "console.log").open("w", encoding="utf-8")
            streams.append(stream)
            arguments = [sys.executable, "-B", "-m", "lab.components", "--role", node["role"], "--name", name,
                         "--config", str(config_path), "--output", str(folder)]
            env = dict(os.environ, ROS_DOMAIN_ID="174", ROS_LOCALHOST_ONLY="1", PYTHONDONTWRITEBYTECODE="1",
                       ROS_LOG_DIR=str(folder / "ros_logs"))
            process = subprocess.Popen(network.command(name, arguments), cwd=ROOT, env=env,
                                       stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            processes[name] = process
            record = {"name": name, "pid": process.pid, "start_ticks": proc_start(process.pid), "config": str(config_path),
                      "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                      "namespace": node["namespace"], "command": arguments}
            records.append(record)
            save(output / "processes.json", records)
            deadline = time.monotonic() + 35
            while not (folder / "ready.json").exists():
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited during startup; see {folder / 'console.log'}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{name} startup timed out")
                time.sleep(.05)
            readiness[name] = json.loads((folder / "ready.json").read_text())
            print("READY", name, node["ip"], flush=True)
        clocks = {r["clock_domain_id"] for r in readiness.values()}
        if len(clocks) != 1:
            raise RuntimeError("Component clocks differ; cross-process subtraction refused")
        save(output / "readiness.json", readiness)
        calibration = []
        for robot in config["robots"]:
            # Ping across the routed uplink even when the application first hop
            # is a same-site edge. This measures both configured directions.
            target = config["nodes"]["worker1"]["ip"]
            result = subprocess.run(network.command(robot["id"], ["ping", "-n", "-c", "3", "-i", "0.2", "-W", "2", target]),
                                    capture_output=True, text=True, timeout=10)
            calibration.append({"robot": robot["id"], "target": target, "returncode": result.returncode, "output": result.stdout, "error": result.stderr})
            if result.returncode:
                raise RuntimeError(f"Route calibration failed for {robot['id']}")
        save(output / "ping_calibration.json", calibration)
        network.snapshot("before")
        if config["capture"]:
            network.start_capture()
        start = time.monotonic_ns() + 1_000_000_000
        begin = start + round(config["warmup_s"] * 1e9)
        end = begin + round(config["duration_s"] * 1e9)
        save(output / "start.json", {"start_ns": start, "measurement_start_ns": begin, "measurement_end_ns": end,
             "drain_deadline_ns": end + round(config["drain_s"] * 1e9), "clock_domain_id": next(iter(clocks))})
        schedule = sorted(config.get("impairment_schedule", []), key=lambda e: e["at_s"])
        next_event = 0
        hard_deadline = end + round((config["drain_s"] + config["request_timeout_s"] + 15) * 1e9)
        robot_names = [r["id"] for r in config["robots"]]
        print(f"MEASURE {config['topology']} {len(robot_names)} independent clients; {config['duration_s']} seconds after warmup", flush=True)
        while any(processes[name].poll() is None for name in robot_names):
            now = time.monotonic_ns()
            for name, process in processes.items():
                code = process.poll()
                if code is not None and (name not in robot_names or code != 0):
                    raise RuntimeError(f"{name} exited unexpectedly with code {code}")
            while next_event < len(schedule) and now >= begin + round(schedule[next_event]["at_s"] * 1e9):
                event = schedule[next_event]
                for direction in (("forward", "reverse") if event["direction"] == "both" else (event["direction"],)):
                    network.apply_profile(event["site"], event["profile"], direction)
                events.append(dict(event, actual_ns=time.monotonic_ns(), scheduled_ns=begin + round(event["at_s"] * 1e9)))
                save(output / "impairment_events.json", events)
                next_event += 1
            cpu.append(cpu_sample(records))
            if now > hard_deadline:
                raise RuntimeError("Robot processes exceeded bounded drain")
            time.sleep(.1)
        if time.monotonic_ns() < end:
            raise RuntimeError("Sources ended before the observation window")
        if any(processes[name].returncode != 0 for name in robot_names):
            raise RuntimeError("A robot process failed")
        save(output / "run_status.json", {"application_run_finished": True, "run_id": run_id,
             "actual_robot_processes": len(robot_names), "actual_worker_processes": config["workers"],
             "active_components": config["active_components"]})
    except BaseException as exc:
        failure = exc
        save(output / "invalid.json", {"reason": repr(exc), "traceback": traceback.format_exc()})
    finally:
        save(output / "cpu.json", cpu)
        save(output / "impairment_events.json", events)
        stopped = stop_processes(records, processes)
        save(output / "process_cleanup.json", stopped)
        for stream in streams:
            stream.close()
        try:
            network.stop_capture()
            network.snapshot("after")
        finally:
            network.cleanup()
    if failure:
        raise failure
    if any(r["forced_kill"] or r["exit_code"] != 0 for r in stopped):
        raise RuntimeError("A component failed or required forced cleanup; inspect retained logs")
    from .analyze import analyze
    report = analyze(output)
    print(json.dumps({"output": str(output), "accounting_passed": report["accounting_passed"],
                      "generated": report["generated"], "successful": report["successful"]}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("doctor", help="Read-only prerequisite check")
    check.add_argument("--config", type=Path, default=ROOT / "configs/full_reference.json")
    run = sub.add_parser("run", help="Run and clean up one isolated experiment")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path)
    for flag in ("warmup", "duration", "drain"):
        run.add_argument("--" + flag, type=float)
    for name in ("analyze", "status", "cleanup"):
        sub.add_parser(name).add_argument("run_directory", type=Path)
    args = parser.parse_args()
    if args.command in ("doctor", "run"):
        config = load_config(args.config)
        if args.command == "doctor":
            validate_config(config)
            result = doctor(config)
            print(json.dumps(result, indent=2))
            return 0 if result["passed"] else 1
        for flag in ("warmup", "duration", "drain"):
            value = getattr(args, flag)
            if value is not None:
                config[flag + "_s"] = value
        output = args.output or ROOT / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("termination requested")))
        run_experiment(config, output)
    elif args.command == "analyze":
        from .analyze import analyze
        print(json.dumps(analyze(args.run_directory), indent=2))
    else:
        folder = args.run_directory.resolve()
        records = json.loads((folder / "processes.json").read_text()) if (folder / "processes.json").exists() else []
        if args.command == "status":
            print(json.dumps([dict(r, owned_process_alive=owned_alive(r)) for r in records], indent=2))
        else:
            if os.geteuid() != 0:
                raise RuntimeError("Cleanup requires root")
            from .network import LabNetwork
            save(folder / "manual_process_cleanup.json", stop_processes(records))
            network = LabNetwork.load(folder)
            network.stop_capture()
            network.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
