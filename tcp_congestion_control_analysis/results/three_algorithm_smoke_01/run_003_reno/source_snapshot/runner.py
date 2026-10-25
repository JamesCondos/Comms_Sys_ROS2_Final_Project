"""Start one experiment, save its measurements, and clean up its network."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .metrics import save_json, summarize
from .network import LabNetwork
from .settings import make_config

ROOT = Path(__file__).resolve().parents[1]


def validate_config(config):
    # Reject invalid settings before changing the network or creating output files.
    if config["protocol"] not in ("h1", "h2", "h3") or config["workload"] not in ("mixed", "random"):
        raise ValueError("Choose h1/h2/h3 and mixed/random traffic")
    for name in ("seconds", "finish_seconds", "request_timeout_s", "load", "mean_service_s", "video_fps"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(name + " must be finite and positive")
    if not math.isfinite(config["warmup_s"]) or config["warmup_s"] < 0:
        raise ValueError("Warm-up must be finite and nonnegative")
    for name in ("queue_capacity", "gateway_concurrency", "edge_concurrency"):
        if type(config[name]) is not int or config[name] < 1:
            raise ValueError(name + " must be a positive integer")
    if config["service_mode"] not in ("fixed", "exponential"):
        raise ValueError("Choose fixed or exponential worker service")
    expected = [{"id": f"robot{i}", "site": "A" if i <= 2 else "B"} for i in range(1, 5)]
    if config["robots"] != expected:
        raise ValueError("The shared topology uses four robots: two per site")
    if config["protocol"] == "h3" and any(value != "cubic" for value in config["tcp_congestion"].values()):
        raise ValueError("Reno/BBR TCP trials require h1 or h2; HTTP/3 uses QUIC")
    for algorithm in config["tcp_congestion"].values():
        if algorithm not in ("reno", "cubic", "bbr"):
            raise ValueError("Choose reno, cubic or bbr")
    profiles = [config["sites"][site][direction] for site in ("A", "B") for direction in ("forward", "reverse")]
    for event in config.get("impairment_schedule", []):
        if event["site"] not in ("A", "B") or event["direction"] not in ("forward", "reverse"):
            raise ValueError("Invalid impairment site or direction")
        if not math.isfinite(event["at_s"]) or not 0 <= event["at_s"] < config["seconds"]:
            raise ValueError("Impairment times must lie inside measurement")
        profiles.append(event["profile"])
    for profile in profiles:
        for field, minimum, maximum in (("delay_ms", 0, 10000), ("loss_pct", 0, 100), ("rate_mbit", .001, 100000)):
            value = profile.get(field, None if field == "rate_mbit" else 0)
            if field == "rate_mbit" and value is None:
                continue
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError("Invalid network " + field)
    if config.get("network_mode") == "loopback":
        if config["capture"] or config.get("impairment_schedule") or any(
                profile.get("delay_ms") or profile.get("loss_pct") or profile.get("rate_mbit") for profile in profiles):
            raise ValueError("Loopback checks cannot apply WAN impairments or capture")


def doctor(config=None):
    # This check only reads the environment; it does not install or load anything.
    commands = {name: shutil.which(name) for name in ("ip", "tc", "ping", "tcpdump", "openssl")}
    modules = {name: importlib.util.find_spec(name) is not None for name in
               ("rclpy", "std_msgs", "sensor_msgs", "httpx", "hypercorn", "aioquic", "h2", "cv2")}
    path = Path("/proc/sys/net/ipv4/tcp_available_congestion_control")
    algorithms = path.read_text().split() if path.exists() else []
    requested = set(config["tcp_congestion"].values()) if config and config["protocol"] != "h3" else set()
    return dict(root=os.geteuid() == 0, commands=commands, modules=modules,
        ethtool=shutil.which("ethtool"), tcp_algorithms=algorithms,
        passed=all(commands.values()) and all(modules.values()) and requested.issubset(algorithms))


def process_identity(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def stop_processes(records, processes=None):
    # Check PID start times before signalling, so recovery cannot kill a reused PID.
    def alive(row):
        try:
            return (row["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                    and process_identity(row["pid"]) == row["start_ticks"]
                    and b"https_protocol_analysis.components" in Path(f"/proc/{row['pid']}/cmdline").read_bytes())
        except FileNotFoundError:
            return False
    for row in reversed(records):
        if alive(row):
            try:
                os.kill(row["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 8
    while any(alive(row) for row in records) and time.monotonic() < deadline:
        time.sleep(.05)
    result = []
    for row in records:
        forced = alive(row)
        if forced:
            try:
                os.kill(row["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        process = (processes or {}).get(row["name"])
        if process:
            process.wait(timeout=5)
        result.append(dict(name=row["name"], forced_kill=forced,
                           exit_code=process.returncode if process else None))
    return result


def run_experiment(requested, output):
    validate_config(requested)
    environment = doctor(requested)
    loopback = requested.get("network_mode") == "loopback"
    if not environment["passed"] or (not environment["root"] and not loopback):
        raise RuntimeError("Prerequisites failed; use the configured Python with sudo for WAN runs: " + json.dumps(environment))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    network_dir = output / "network"
    network_dir.mkdir()
    config = dict(requested, run_id=uuid.uuid4().hex, start_file=str(output / "start.json"))
    save_json(output / "environment.json", environment)
    config["active_components"] = ["worker1", "worker2", "proxy", "edgeA", "edgeB", "robot1", "robot2", "robot3", "robot4"]
    network = None
    processes, records, streams, failures = {}, [], [], []
    try:
        if loopback:
            # This mode verifies ROS/HTTP locally; it is never labelled as a WAN measurement.
            config["nodes"] = {name: dict(role="worker" if name.startswith("worker") else
                "robot" if name.startswith("robot") else "edge" if name.startswith("edge") else "proxy",
                ip=f"127.0.0.{index + 10}", site="A" if name in ("edgeA", "robot1", "robot2") else "B")
                for index, name in enumerate(config["active_components"])}
        else:
            network = LabNetwork(config, network_dir)
            network.setup()
            config["nodes"] = network.nodes
            network.snapshot("before")
            if config["capture"]:
                network.start_capture()

        # One temporary certificate covers this run's virtual host addresses.
        cert, key = output / "certificate.pem", output / "private_key.pem"
        names = "DNS:complex.local," + ",".join("IP:" + node["ip"] for node in config["nodes"].values())
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=complex.local",
            "-addext", "subjectAltName=" + names], check=True, capture_output=True)
        key.chmod(0o600)
        config.update(certfile=str(cert), keyfile=str(key), cafile=str(cert))
        save_json(output / "resolved.json", config)
        snapshots = output / "source_snapshot"
        snapshots.mkdir()
        hashes = {}
        source_files = list(Path(__file__).parent.glob("*.py")) + [Path(__file__).with_name("requirements.txt")]
        for source in source_files:
            data = source.read_bytes()
            (snapshots / source.name).write_bytes(data)
            hashes[source.name] = hashlib.sha256(data).hexdigest()
        save_json(output / "source_sha256.json", hashes)

        # Start backends before clients. Readiness files are written after ROS discovery.
        for name in config["active_components"]:
            console = (output / (name + ".log")).open("x")
            streams.append(console)
            args = [sys.executable, "-B", "-m", "https_protocol_analysis.components", "--name", name,
                    "--config", str(output / "resolved.json"), "--output", str(output / "logs" / name)]
            command = args if loopback else network.command(name, args)
            env = dict(os.environ, ROS_DOMAIN_ID="186", ROS_LOCALHOST_ONLY="1", PYTHONDONTWRITEBYTECODE="1",
                       ROS_LOG_DIR=str(output / "ros_logs" / name))
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=console, stderr=subprocess.STDOUT)
            processes[name] = process
            records.append(dict(name=name, pid=process.pid, start_ticks=process_identity(process.pid),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
            save_json(output / "processes.json", records)
            deadline = time.monotonic() + 35
            ready = output / "logs" / name / "ready.json"
            while not ready.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(name + " failed startup; inspect " + str(output / (name + ".log")))
                time.sleep(.05)
            clock = Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + os.readlink("/proc/self/ns/time")
            if json.loads(ready.read_text())["clock_domain_id"] != clock:
                raise RuntimeError("Components do not share a monotonic clock domain")
            print("READY", name, flush=True)

        start = time.monotonic_ns() + 1_000_000_000
        begin = start + round(config["warmup_s"] * 1e9)
        end = begin + round(config["seconds"] * 1e9)
        drain = end + round(config["finish_seconds"] * 1e9)
        save_json(output / "start.json", dict(start_ns=start, measurement_start_ns=begin,
            measurement_end_ns=end, drain_deadline_ns=drain))
        schedule = sorted(config.get("impairment_schedule", []), key=lambda row: row["at_s"])
        applied, index = [], 0
        robots = [row["id"] for row in config["robots"]]
        while any(processes[name].poll() is None for name in robots):
            for name, process in processes.items():
                code = process.poll()
                if code is not None and (name not in robots or code != 0):
                    raise RuntimeError(name + " exited unexpectedly")
            now = time.monotonic_ns()
            while index < len(schedule) and now >= begin + round(schedule[index]["at_s"] * 1e9):
                event = schedule[index]
                network.apply_profile(event["site"], event["direction"], event["profile"])
                applied.append(dict(event, actual_ns=time.monotonic_ns()))
                save_json(output / "impairment_events.json", applied)
                index += 1
            if now > drain + 10_000_000_000:
                raise RuntimeError("Robots exceeded the bounded drain")
            time.sleep(.05)
        if time.monotonic_ns() < end or any(processes[name].returncode != 0 for name in robots):
            raise RuntimeError("Sources did not finish their complete measurement window")
    except BaseException as exc:
        failures.append(repr(exc))
    finally:
        stopped = stop_processes(records, processes)
        save_json(output / "process_cleanup.json", stopped)
        if any(row["forced_kill"] or row["exit_code"] != 0 for row in stopped):
            failures.append("A process failed or required forced termination")
        for stream in streams:
            stream.close()
        if network:
            try:
                if config["capture"]:
                    network.stop_capture()
                if network.nodes:
                    network.snapshot("after")
            except Exception as exc:
                failures.append(repr(exc))
            finally:
                try:
                    network.cleanup()
                except Exception as exc:
                    failures.append(repr(exc))
        else:
            save_json(network_dir / "cleanup.json", {"success": True, "note": "Loopback check; no network resources created"})
    if failures:
        save_json(output / "invalid.json", {"errors": failures})
        raise RuntimeError("Run retained with errors: " + "; ".join(failures))
    return summarize(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "run", "cleanup"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--protocol", choices=("h1", "h2", "h3"), default="h2")
    parser.add_argument("--profile", choices=("clean", "delay", "loss", "rate", "combined"), default="clean")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--load", type=float, default=148)
    parser.add_argument("--loopback", action="store_true", help="Local functional check; no WAN or impairments")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json.dumps(doctor(), indent=2))
        return
    if args.command == "cleanup":
        records = json.loads((args.output / "processes.json").read_text())
        stop_processes(records)
        network = LabNetwork.load(args.output / "network")
        network.recover_captures()
        network.cleanup()
        return
    config = json.loads(args.config.read_text()) if args.config else make_config("http", args.protocol, args.profile, args.load, args.seconds)
    if args.loopback:
        config.update(network_mode="loopback", capture=False)
    output = args.output or Path(__file__).parent / "results" / datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("Termination requested")))
    print(json.dumps(run_experiment(config, output), indent=2))


if __name__ == "__main__":
    main()
