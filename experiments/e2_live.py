"""E2B: measured ROS2/HTTP arrival variability with exact queue accounting.

This additive runner preserves all E1 files. One shared FIFO gateway, one
persistent HTTP/1.1 connection and one server worker are fixed across patterns.
"""
import argparse
from collections import Counter, deque
import hashlib
import http.client
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time
import traceback
import uuid

from baseline import save_csv, save_json, percentile
from e1_extended import (read_csv, digest, serve, wait_json, host_metadata,
                         resolved_run_config as e1_resolved, summarize as e1_summarize,
                         validate_config as e1_validate)
from e2_source import PATTERNS, generator_process, make_schedule, wait_until_ns

def validate_config(config):
    e1_validate(config)
    if config["actual_source_counts"] != [4]:
        raise ValueError("E2B fixes four actual sources")
    if {c["id"] for c in config["conditions"]} != set(PATTERNS):
        raise ValueError("E2B requires exactly the four specified arrival conditions")
    for condition in config["conditions"]:
        if condition.get("arrival_pattern", condition["id"]) != condition["id"]:
            raise ValueError("Condition ID must identify its arrival pattern")
        if set(condition) - {"id", "arrival_pattern", "source_counts"}:
            raise ValueError("Conditions may vary arrival pattern only")
    if config.get("network_mode") != "dedicated_veth" or config.get("network_rate_mbit") is not None:
        raise ValueError("All E2B cases use the same unshaped dedicated veth path")
    if config.get("gateway_dispatch_delay_ms", 0) != 0 or config["server_processing_delay_ms"] < 0:
        raise ValueError("Invalid fixed service setup")
    model = config["bursty_model"]
    if not 0 < model["on_s"] < model["cycle_s"]:
        raise ValueError("ON duration must lie strictly inside each cycle")
    for name in ("warmup_s", "duration_s"):
        cycles = config[name] / model["cycle_s"]
        if abs(cycles - round(cycles)) > 1e-9:
            raise ValueError("Warmup and measurement must contain whole burst cycles")


def resolved_run_config(sweep, condition, count, repeat, folder):
    fixed = dict(condition, gateway_dispatch_delay_ms=0,
                 server_processing_delay_ms=sweep["server_processing_delay_ms"], network_rate_mbit=None)
    config = e1_resolved(sweep, fixed, count, repeat, folder)
    config.update(experiment="E2", subexperiment="E2B_realistic_arrival_variability", schema_version=4,
                  arrival_pattern=condition["id"],
                  queue_system_boundary="gateway FIFO admission to worker completion after response read and acknowledgement parse",
                  queue_wait_boundary="gateway FIFO admission to worker dequeue",
                  service_boundary="worker dequeue to response parse and locked completion state update",
                  e9_prerequisite_deviation="E9 not yet performed; shared E1 gateway arrangement held fixed, not selected by E9",
                  source_implementation="four ROS nodes in one open-loop publishing process")
    return config


def statistics(values):
    if not values:
        return {"n": 0, "mean": None, "sd": None, "cv2": None, "p50": None, "p95": None, "p99": None, "max": None}
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return {"n": len(values), "mean": mean, "sd": math.sqrt(variance),
            "cv2": variance / mean ** 2 if mean else None,
            "p50": percentile(values, 50), "p95": percentile(values, 95),
            "p99": percentile(values, 99), "max": max(values)}


def occupancy_area(events, begin, end, field):
    """Integer-nanosecond integration of a right-continuous state trace."""
    area, previous, state, maximum = 0, begin, 0, 0
    for row in events:
        timestamp, next_state = int(row["timestamp_ns"]), int(row[field])
        if timestamp < begin:
            state = next_state
            continue
        if timestamp >= end:
            break
        area += state * (timestamp - previous)
        maximum = max(maximum, state, next_state)
        state, previous = next_state, timestamp
    return area + state * (end - previous), max(maximum, state), state


def population_identity(rows, begin, end, finish):
    """Exact finite-window identity, with admission-cohort carry-out and carry-in."""
    cohort, overlap, carry_in, carry_out = [], 0, 0, 0
    for row in rows:
        arrival, departure = int(row["enqueue_ns"]), int(row[finish])
        clipped = max(0, min(departure, end) - max(arrival, begin))
        overlap += clipped
        if arrival < begin:
            carry_in += clipped
        elif arrival < end:
            cohort.append(departure - arrival)
            carry_out += max(0, departure - end)
    full = sum(cohort)
    return {"arrival_count": len(cohort), "residence_sum_ns": full,
            "overlap_area_ns": overlap, "carry_in_area_ns": carry_in,
            "carry_out_area_ns": carry_out, "identity_residual_ns": overlap - full - carry_in + carry_out,
            "uncorrected_lambda_W": full / (end - begin),
            "boundary_correction": (carry_in - carry_out) / (end - begin),
            "corrected_lambda_W": (full + carry_in - carry_out) / (end - begin)}


def summarize(folder):
    result = e1_summarize(folder)
    config = json.loads((folder / "config.json").read_text())
    generated, rows, events = [read_csv(folder / name) for name in ("generated.csv", "messages.csv", "queue.csv")]
    begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
    duration = (end - begin) / 1e9
    admitted = [r for r in rows if r["outcome"] != "queue_drop"]
    if any(r["outcome"] != "ok" for r in admitted):
        raise ValueError("E2 queue calibration requires fully resolved successful admissions")
    cohort = [r for r in admitted if begin <= int(r["enqueue_ns"]) < end]
    system_area, max_system, final_system = occupancy_area(events, begin, end, "system_length")
    queue_area, max_queue, final_queue = occupancy_area(events, begin, end, "queue_length")
    busy_area, _, final_busy = occupancy_area(events, begin, end, "in_flight")
    system_identity = population_identity(admitted, begin, end, "response_ns")
    queue_identity = population_identity(admitted, begin, end, "dequeue_ns")
    W = statistics([(int(r["response_ns"]) - int(r["enqueue_ns"])) / 1e6 for r in cohort])
    Wq = statistics([(int(r["dequeue_ns"]) - int(r["enqueue_ns"])) / 1e6 for r in cohort])
    S = statistics([(int(r["response_ns"]) - int(r["dequeue_ns"])) / 1e6 for r in cohort])
    result.update(experiment="E2", subexperiment="E2B", schema_version=4, arrival_pattern=config["arrival_pattern"],
                  queue_system={"arrival_rate_rps": len(cohort) / duration,
                    "completed_rps": sum(begin <= int(r["response_ns"]) < end for r in admitted) / duration,
                    "arrivals": len(cohort), "mean_L": system_area / (end - begin),
                    "initial_population": sum(int(r["enqueue_ns"]) < begin <= int(r["response_ns"]) for r in admitted),
                    "departures": sum(begin <= int(r["response_ns"]) < end for r in admitted),
                    "final_population": sum(int(r["enqueue_ns"]) < end <= int(r["response_ns"]) for r in admitted),
                    "mean_Lq": queue_area / (end - begin), "busy_fraction": busy_area / (end - begin),
                    "system_area_ns": system_area, "queue_area_ns": queue_area, "busy_area_ns": busy_area,
                    "max_L": max_system, "max_Lq": max_queue,
                    "end_L": final_system, "end_Lq": final_queue, "end_busy": final_busy,
                    "W_ms": W, "Wq_ms": Wq, "S_ms": S,
                    "little_system": system_identity, "little_queue": queue_identity,
                    "event_vs_residence_system_residual_ns": system_area - system_identity["overlap_area_ns"],
                    "event_vs_residence_queue_residual_ns": queue_area - queue_identity["overlap_area_ns"],
                    "population_identity_residual_ns": system_area - queue_area - busy_area},
                  arrival_variability={}, source_jitter={})
    for label, records, field in (("scheduled", generated, "scheduled_ns"),
                                   ("source", generated, "source_ns"), ("gateway", rows, "enqueue_ns")):
        timestamps = sorted(int(r[field]) for r in records if begin <= int(r[field]) < end)
        iat = [(b - a) / 1e6 for a, b in zip(timestamps, timestamps[1:])]
        bins = [0] * int(math.ceil(duration))
        for timestamp in timestamps:
            bins[min(len(bins) - 1, int((timestamp - begin) // 1_000_000_000))] += 1
        count_stats = statistics(bins)
        result["arrival_variability"][label] = {"interarrival_ms": statistics(iat),
             "one_second_bin_counts": bins,
             "one_second_count_fano": count_stats["sd"] ** 2 / count_stats["mean"] if count_stats["mean"] else None}
        short_bins = [0] * int(math.ceil(duration * 10))
        for timestamp in timestamps:
            short_bins[min(len(short_bins) - 1, int((timestamp - begin) // 100_000_000))] += 1
        short_stats = statistics(short_bins)
        result["arrival_variability"][label].update(hundred_ms_bin_counts=short_bins,
            hundred_ms_count_fano=short_stats["sd"] ** 2 / short_stats["mean"] if short_stats["mean"] else None)
    generated_cohort = [r for r in generated if begin <= int(r["scheduled_ns"]) < end]
    for name, first, last in (("schedule_to_source_ms", "scheduled_ns", "source_ns"),
                              ("schedule_to_publish_ms", "scheduled_ns", "publish_start_ns"),
                              ("publish_call_ms", "publish_start_ns", "publish_end_ns")):
        result["source_jitter"][name] = statistics([(int(r[last]) - int(r[first])) / 1e6 for r in generated_cohort])
    result["source_jitter"]["published_after_measurement_end"] = sum(int(r["source_ns"]) >= end for r in generated_cohort)
    save_json(folder / "summary.json", result)
    return result


def run(config, folder):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String
    import e2_source as generator

    folder.mkdir(parents=True, exist_ok=False)
    records, events, cpu_rows = [], [], []
    fifo, condition = deque(), threading.Condition()
    stopping, stop_cpu = threading.Event(), threading.Event()
    busy = 0
    network = server = source = node = executor = None
    spin = sender = cpu_thread = None
    server_log = None
    failure = None
    capture = bool(config.get("capture", False))
    initialized = False

    def event(timestamp, action, message_id):
        events.append({"timestamp_ns": timestamp, "queue_length": len(fifo), "in_flight": busy,
                       "system_length": len(fifo) + busy, "action": action, "id": message_id})

    def receive(msg, ros_topic):
        callback_ns = time.monotonic_ns()
        message = json.loads(msg.data)
        row = {k: v for k, v in message.items() if k != "payload"}
        row.update(callback_ns=callback_ns, enqueue_ns=None, dequeue_ns=None, request_start_ns=None,
                   response_ns=None, response_body_received_ns=None, server_receive_ns=None, server_processing_start_ns=None,
                   server_processed_ns=None, http_status=None, connection_id=None, http_version=None,
                   outcome="pending", error="", request_body_bytes=0, retry_count=0, ros_topic=ros_topic)
        with condition:
            row["enqueue_ns"] = time.monotonic_ns()
            records.append(row)
            if len(fifo) >= config["queue_capacity"]:
                row["outcome"] = "queue_drop"
                action = "drop"
            else:
                fifo.append((message, row))
                action = "admit"
            event(row["enqueue_ns"], action, row["id"])
            condition.notify()

    def send():
        nonlocal busy
        connection = http.client.HTTPConnection(config["server_host"], config["server_port"], timeout=config["request_timeout_s"])
        try:
            while True:
                with condition:
                    condition.wait_for(lambda: fifo or stopping.is_set())
                    if stopping.is_set():
                        break
                    message, row = fifo.popleft()
                    row["dequeue_ns"] = time.monotonic_ns()
                    busy = 1
                    event(row["dequeue_ns"], "dequeue", row["id"])
                try:
                    if config["gateway_dispatch_delay_ms"]:
                        time.sleep(config["gateway_dispatch_delay_ms"] / 1000)
                    body = json.dumps(message, separators=(",", ":")).encode()
                    row["request_body_bytes"] = len(body)
                    row["request_start_ns"] = time.monotonic_ns()
                    connection.request("POST", f"/robots/{message['robot_id']}/{message['message_type']}",
                                       body=body, headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    response_body = response.read()
                    row["response_body_received_ns"] = time.monotonic_ns()
                    ack = json.loads(response_body)
                    row.update(http_status=response.status, http_version=response.version)
                    for field in ("server_receive_ns", "server_processing_start_ns", "server_processed_ns", "connection_id"):
                        row[field] = ack[field]
                    row["outcome"] = "ok" if response.status == 200 and ack["id"] == message["id"] else "http_error"
                except Exception as exc:
                    row.update(outcome="request_error", error=repr(exc))
                    connection.close()
                finally:
                    with condition:
                        # Response completion and state change share one timestamp and lock.
                        # This permits exact reconciliation with the item-residence integral.
                        row["response_ns"] = time.monotonic_ns()
                        if row["response_ns"] > config.get("drain_deadline_ns", float("inf")):
                            row["outcome"] = "pending"
                            row["error"] = "Response completed after bounded drain"
                        busy = 0
                        event(row["response_ns"], "complete", row["id"])
        finally:
            connection.close()

    try:
        if config.get("network_mode", "dedicated_veth") == "dedicated_veth":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
            from e1_network import NetworkHarness
            network = NetworkHarness(folder, rate_mbit=config.get("network_rate_mbit"))
            network.setup()
            verification = network.verify()
            config.update(server_host=network.server_ip, server_bind=network.server_ip,
                          server_command_prefix=network.server_prefix, network_verification=verification)
            network.snapshot("before")
        else:
            if config.get("network_rate_mbit") is not None:
                raise ValueError("Network shaping requires dedicated_veth")
            config.setdefault("server_host", "127.0.0.1")
            config.setdefault("server_bind", "127.0.0.1")
            config.setdefault("server_command_prefix", [])
        config["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        config["metrics_sha256"] = hashlib.sha256(Path(__file__).with_name("metrics.py").read_bytes()).hexdigest()
        config["ros_traffic_sha256"] = hashlib.sha256(Path(generator.__file__).read_bytes()).hexdigest()
        config["baseline_helpers_sha256"] = hashlib.sha256(Path(__file__).with_name("baseline.py").read_bytes()).hexdigest()
        config["e1_helpers_sha256"] = hashlib.sha256(Path(__file__).with_name("e1_extended.py").read_bytes()).hexdigest()
        save_json(folder / "config.json", config)
        server_log = (folder / "server_process.log").open("w")
        server_command = config["server_command_prefix"] + [sys.executable, str(Path(__file__).resolve()),
            "--server", "--config", str(folder / "config.json"), "--output", str(folder)]
        server = subprocess.Popen(server_command, stdout=server_log, stderr=subprocess.STDOUT)
        server_ready = wait_json(folder / "server_ready.json", server)
        if server_ready["host_metadata"]["clock_domain_id"] != config["host_metadata"]["clock_domain_id"]:
            raise RuntimeError("Source/gateway and server do not share a verified clock domain")
        config["server_port"] = server_ready["port"]
        config["server_pid"] = server_ready["pid"]
        config["server_host_metadata"] = server_ready["host_metadata"]
        if capture and network:
            network.start_capture(config["server_port"])

        ctx = mp.get_context("spawn")
        start, ready = ctx.Value("d", 0), ctx.Queue()
        prefix = "/e2_live/" + folder.name.replace("-", "_")
        topics = generator.topic_specs(config, prefix)
        save_json(folder / "topics.json", topics)
        source = ctx.Process(target=generator_process, args=(config, str(folder), prefix, start, ready))
        rclpy.init()
        initialized = True
        node = rclpy.create_node("e2_live_gateway")
        qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST)
        for spec in topics:
            node.create_subscription(String, spec["topic"], lambda msg, topic=spec["topic"]: receive(msg, topic), qos)
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        spin, sender = threading.Thread(target=executor.spin), threading.Thread(target=send)
        spin.start()
        sender.start()
        source.start()
        config["source_pid"], config["gateway_pid"] = source.pid, os.getpid()

        def sample_cpu():
            ticks = os.sysconf("SC_CLK_TCK")
            page_size = os.sysconf("SC_PAGE_SIZE")
            previous = {name: (0.0, 0) for name in ("server", "gateway", "source")}
            while True:
                sample = {}
                for name in previous:
                    try:
                        fields = Path(f"/proc/{config[name + '_pid']}/stat").read_text().rsplit(")", 1)[1].split()
                        previous[name] = ((int(fields[11]) + int(fields[12])) / ticks, int(fields[21]) * page_size)
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                    sample[f"{name}_cpu_s"], sample[f"{name}_rss_bytes"] = previous[name]
                sample["timestamp_ns"] = time.monotonic_ns()
                cpu_rows.append(sample)
                if stop_cpu.wait(config["cpu_sample_interval_s"]):
                    break
        cpu_thread = threading.Thread(target=sample_cpu)
        cpu_thread.start()
        ready.get(timeout=30)
        graph_deadline = time.monotonic() + 10
        while True:
            actual_nodes = sorted([{"name": name, "namespace": ns} for name, ns in node.get_node_names_and_namespaces()
                                   if name == "traffic_source" and ns.startswith(prefix + "/")], key=lambda x: x["namespace"])
            publisher_counts = {spec["topic"]: len(node.get_publishers_info_by_topic(spec["topic"])) for spec in topics}
            if len(actual_nodes) == config["actual_sources"] and all(n == 1 for n in publisher_counts.values()):
                break
            if time.monotonic() > graph_deadline:
                raise RuntimeError("Actual ROS source nodes/topics do not match configured counts")
            time.sleep(0.1)
        save_json(folder / "ros_graph.json", {"actual_nodes": actual_nodes, "topic_publisher_counts": publisher_counts,
                   "configured_topics": topics, "verified_ns": time.monotonic_ns()})
        config["actual_launched_source_nodes"] = len(actual_nodes)
        config["actual_publisher_topics"] = len(publisher_counts)
        config["rmw_implementation"] = rclpy.utilities.get_rmw_implementation_identifier()
        start.value = time.monotonic() + 0.5
        source_start_ns = int(start.value * 1e9)
        begin = source_start_ns + int(config["warmup_s"] * 1e9)
        end = begin + int(config["duration_s"] * 1e9)
        config.update(source_start_ns=source_start_ns, measurement_start_ns=begin,
                      measurement_end_ns=end, drain_deadline_ns=end + int(config["drain_s"] * 1e9))
        config["config_sha256"] = digest(config)
        save_json(folder / "config.json", config)
        source.join(timeout=config["warmup_s"] + config["duration_s"] + 10)
        if source.is_alive() or source.exitcode != 0:
            raise RuntimeError("Traffic generator did not finish successfully")
        # Observation duration is fixed independently of the last publication.
        wait_until_ns(end)
        generated_count = len(read_csv(folder / "generated.csv"))
        while time.monotonic_ns() < config["drain_deadline_ns"]:
            if server.poll() is not None:
                raise RuntimeError("Server exited during run")
            with condition:
                if not fifo and not busy and len(records) == generated_count:
                    break
            time.sleep(0.05)
        save_json(folder / "drain.json", {"observed_end_ns": time.monotonic_ns(), "deadline_ns": config["drain_deadline_ns"],
                   "waiting": len(fifo), "in_flight": busy, "generated_all": generated_count,
                   "gateway_received_all": len(records)})
    except BaseException as exc:
        failure = exc
        save_json(folder / "invalid.json", {"reason": repr(exc), "traceback": traceback.format_exc(),
                   "infrastructure_invalid": True})
    finally:
        stopping.set()
        with condition:
            condition.notify_all()
        if executor is not None:
            executor.shutdown(timeout_sec=3)
        if spin is not None and spin.ident:
            spin.join(timeout=3)
        if sender is not None and sender.ident:
            sender.join(timeout=config["request_timeout_s"] + config["gateway_dispatch_delay_ms"] / 1000 + 3)
        if source is not None and source.is_alive():
            source.terminate()
            source.join(timeout=5)
        stop_cpu.set()
        if cpu_thread is not None and cpu_thread.ident:
            cpu_thread.join(timeout=3)
        if server is not None:
            (folder / "server.stop").touch()
            try:
                server.wait(timeout=config["request_timeout_s"] + 5)
            except subprocess.TimeoutExpired:
                # Target the server process in its owned namespace, not an unrelated host process.
                if config.get("server_pid"):
                    subprocess.run(config.get("server_command_prefix", []) + ["kill", "-TERM", str(config["server_pid"])], check=False)
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                failure = failure or RuntimeError("Server required forced shutdown")
        if server_log is not None:
            server_log.close()
        if node is not None:
            node.destroy_node()
        if initialized:
            rclpy.shutdown()
        save_csv(folder / "messages.csv", records)
        save_csv(folder / "queue.csv", events)
        save_csv(folder / "cpu.csv", cpu_rows)
        if network is not None:
            try:
                if capture:
                    network.stop_capture()
                network.snapshot("after")
            finally:
                network.cleanup()
    if failure is not None:
        raise failure
    if server.returncode != 0 or sender.is_alive():
        raise RuntimeError("Unclean server/sender shutdown")
    try:
        result = summarize(folder)
        from validate_e2_live import validate_run
        validity = validate_run(folder)
        save_json(folder / "validation.json", validity)
        return result
    except Exception as exc:
        save_json(folder / "invalid.json", {"reason": repr(exc), "traceback": traceback.format_exc(),
                   "infrastructure_invalid": True, "stage": "offline_summary_or_validation"})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    config, folder = json.loads(args.config.read_text()), args.output.resolve()
    if args.server:
        serve(config, folder)
        return
    validate_config(config)
    folder.mkdir(parents=True, exist_ok=args.resume)
    if args.resume and (folder / "sweep_config.json").exists():
        if json.loads((folder / "sweep_config.json").read_text()) != config:
            raise ValueError("Resume configuration differs from frozen sweep")
    else:
        save_json(folder / "sweep_config.json", config)
    snapshots = {"runner_snapshot.py": Path(__file__),
                 "traffic_generator_snapshot.py": Path(__file__).with_name("e2_source.py"),
                 "baseline_helpers_snapshot.py": Path(__file__).with_name("baseline.py"),
                 "metrics_snapshot.py": Path(__file__).with_name("metrics.py"),
                 "e1_helpers_snapshot.py": Path(__file__).with_name("e1_extended.py"),
                 "validator_snapshot.py": Path(__file__).with_name("validate_e2_live.py"),
                 "network_harness_snapshot.py": Path(__file__).resolve().parents[1] / "scripts" / "e1_network.py"}
    for name, path in snapshots.items():
        destination = folder / name
        if args.resume and destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ValueError(f"Resume code differs: {name}")
        destination.write_bytes(path.read_bytes())
    plan = [(c["id"], 4, repeat) for repeat in range(1, config["repeats"] + 1) for c in config["conditions"]]
    random.Random(config["seed"]).shuffle(plan)
    save_json(folder / "run_order.json", plan)
    completed = set()
    for path in folder.glob("*/validation.json"):
        if json.loads(path.read_text()).get("passed") and not (path.parent / "invalid.json").exists():
            previous = json.loads((path.parent / "config.json").read_text())
            completed.add((previous["condition_id"], previous["actual_sources"], previous["repeat"]))
    conditions = {c["id"]: c for c in config["conditions"]}
    for condition_id, count, repeat in plan:
        if (condition_id, count, repeat) in completed:
            continue
        run_folder = folder / f"{condition_id}_n{count:03d}_r{repeat:02d}_{uuid.uuid4().hex[:8]}"
        print(f"START E2B pattern={condition_id} repeat={repeat} {run_folder}", flush=True)
        result = run(resolved_run_config(config, conditions[condition_id], count, repeat, run_folder), run_folder)
        q = result["queue_system"]
        print(f"DONE E2B pattern={condition_id} arrival={q['arrival_rate_rps']:.2f}/s "
              f"busy={q['busy_fraction']:.3f} Lq={q['mean_Lq']:.3f} W_p95={q['W_ms']['p95']:.3f}ms "
              f"drops={result['queue_drops']}", flush=True)
        time.sleep(config.get("inter_run_pause_s", 1))
    from validate_e2_live import validate_sweep
    validation = validate_sweep(folder)
    print(json.dumps({"runs_checked": validation["runs_checked"], "all_passed": validation["all_passed"]}), flush=True)


if __name__ == "__main__":
    main()

