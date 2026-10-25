"""ROS sources, FIFO forwarding queues and processing workers."""

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import random
import signal
import threading
import time

from .metrics import save_json
from .transport import create_client, serve
from .video_frames import load_video_frames

TELEMETRY = [("command", 2, 100), ("status", 5, 100),
             ("odometry", 20, 250), ("sensor", 5, 20000)]


def message(identity, topic, payload, metadata=None):
    # Keep the useful payload size separate from JSON and base64 overhead.
    return json.dumps(dict(metadata or {}, id=identity, topic=topic,
        payload_bytes=len(payload), payload_b64=base64.b64encode(payload).decode())).encode()


def schedule(config, robot):
    if config["workload"] == "random":
        streams = [("probe", config["load"] / 4, 256)]
    else:
        streams = list(TELEMETRY)
        if config.get("video_enabled", True):
            streams.append(("video", config["video_fps"], 0))
        scale = config["load"] / (4 * sum(rate for _, rate, _ in streams))
        streams = [(kind, rate * scale, size) for kind, rate, size in streams]

    # Prepare the entire open-loop schedule before timing starts.
    events = []
    horizon = config.get("warmup_s", 0) + config["seconds"]
    for kind, rate, size in streams:
        rng = random.Random(f"{config['seed']}:{robot}:{kind}")
        seq = 0
        due = rng.expovariate(rate) if config["workload"] == "random" else rng.random() / rate
        while due < horizon:
            events.append((due, kind, seq, size))
            seq += 1
            due += rng.expovariate(rate) if config["workload"] == "random" else 1 / rate
    return sorted(events)


class Node:
    def __init__(self, config, name, output):
        self.config, self.name = config, name
        self.address = config["nodes"][name]
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.queue = asyncio.Queue(config["queue_capacity"])
        self.clients, self.tasks, self.active, self.logs = [], [], {}, {}
        self.drain_deadline = None
        self.stop = asyncio.Event()
        self.accepting = True
        self.clock = Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + os.readlink("/proc/self/ns/time")
        self.event("initial", None)

    def log(self, name, row):
        # Buffer small JSON records; payload contents never go into measurement logs.
        if name not in self.logs:
            self.logs[name] = (self.output / (name + ".jsonl")).open("x", buffering=65536)
        self.logs[name].write(json.dumps(dict(row, time_ns=row.get("time_ns", time.monotonic_ns()))) + "\n")

    def event(self, action, job):
        stamp = time.monotonic_ns()
        self.log("queue", dict(event=action, id=job["meta"]["id"] if job else None,
            time_ns=stamp, queue_len=self.queue.qsize(), in_service=len(self.active)))
        return stamp

    def ready(self, **details):
        save_json(self.output / "ready.json", dict(name=self.name, clock_domain_id=self.clock, **details))

    def remaining(self):
        # Every stage uses the same absolute drain deadline once the run has started.
        start_file = Path(self.config["start_file"])
        if start_file.exists():
            timing = json.loads(start_file.read_text())
            self.drain_deadline = timing["drain_deadline_ns"]
            return max(0.001, (self.drain_deadline - time.monotonic_ns()) / 1e9)
        return self.config["finish_seconds"]

    async def forward_to(self, targets):
        for target, count in targets:
            client = await create_client(self.config["protocol"], self.config["nodes"][target]["ip"],
                8443, self.config["cafile"], max_concurrency=count,
                algorithm=self.config["tcp_congestion"][self.address["role"]],
                callback=lambda row: self.log("connections", row), owner=self.name,
                timeout=self.config["request_timeout_s"])
            self.clients.append(client)
            for _ in range(count):
                self.tasks.append(asyncio.create_task(self.forward(client)))

    def admit(self, body, identity=None):
        # Put complete messages into the FIFO. Overflow drops the arriving message.
        metadata = json.loads(body)
        metadata.pop("payload_b64")
        if identity is not None and identity != metadata["id"]:
            raise ValueError("Header and message IDs differ")
        reply = asyncio.get_running_loop().create_future()
        job = dict(body=body, meta=metadata, reply=reply, admission_ns=None, dequeue_ns=None,
                   request_ns=None, response_ns=None, terminal_ns=None)
        if not self.accepting or self.queue.full():
            self.event("queue_drop", job)
            self.finish_job(job, 503, b"Queue unavailable", "queue_drop")
        else:
            self.queue.put_nowait(job)
            job["admission_ns"] = self.event("admit", job)
        return reply

    def finish_job(self, job, status, body, outcome, error="", ack=None):
        # Resolve the queue item exactly once, including errors and drain timeouts.
        if job["terminal_ns"] is not None:
            return
        self.active.pop(job["meta"]["id"], None)
        job["terminal_ns"] = self.event("service_end" if job["dequeue_ns"] else "terminal", job)
        row = dict(job["meta"], admission_ns=job["admission_ns"], dequeue_ns=job["dequeue_ns"],
            request_ns=job["request_ns"], response_ns=job["response_ns"], terminal_ns=job["terminal_ns"],
            outcome=outcome, http_status=status, error=error)
        if ack:
            row.update(worker_id=ack["worker_id"], worker_processed_ns=ack["worker_processed_ns"])
        self.log("gateway" if self.address["role"] == "robot" else "hops", row)
        if not job["reply"].done():
            job["reply"].set_result((status, body))
        if job["admission_ns"] is not None:
            self.queue.task_done()

    async def forward(self, client):
        while True:
            job = await self.queue.get()
            self.active[job["meta"]["id"]] = job
            job["dequeue_ns"] = self.event("service_start", job)
            status, body, outcome, error, ack = 502, b"Request failed", "request_error", "", None
            try:
                job["request_ns"] = time.monotonic_ns()
                timeout = min(self.config["request_timeout_s"], self.remaining())
                status, body = await asyncio.wait_for(client.post(job["body"], job["meta"]["id"]), timeout)
                job["response_ns"] = time.monotonic_ns()
                outcome = "http_error"
                if status == 200:
                    ack = json.loads(body)
                    if ack["id"] != job["meta"]["id"] or ack["outcome"] != "processed":
                        raise ValueError("Worker acknowledgement does not match the message")
                    if ack["clock_domain_id"] != self.clock:
                        raise ValueError("Worker uses a different clock")
                    outcome = "success"
                    if self.drain_deadline and job["response_ns"] > self.drain_deadline:
                        outcome = "drain_timeout"
            except asyncio.CancelledError:
                self.finish_job(job, 504, b"Drain ended", "drain_timeout")
                raise
            except Exception as exc:
                job["response_ns"] = time.monotonic_ns()
                outcome = "timeout" if isinstance(exc, TimeoutError) else "request_error"
                status = 504 if outcome == "timeout" else 502
                error = repr(exc)
            self.finish_job(job, status, body, outcome, error, ack if outcome == "success" else None)

    async def finish(self):
        self.accepting = False
        try:
            await asyncio.wait_for(self.queue.join(), self.remaining())
        except asyncio.TimeoutError:
            pass
        # Stop in-service jobs, then explicitly account for all still-waiting jobs.
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        while not self.queue.empty():
            self.finish_job(self.queue.get_nowait(), 504, b"Drain ended", "drain_timeout")
        await asyncio.gather(*(client.close() for client in self.clients))

    def close_logs(self, **details):
        save_json(self.output / "final.json", dict(pending=self.queue.qsize() + len(self.active), **details))
        for stream in self.logs.values():
            stream.close()

    async def run_server(self):
        worker = self.address["role"] == "worker"
        if not worker:
            targets = [("worker1", 1), ("worker2", 1)] if self.name == "proxy" else [("proxy", self.config["edge_concurrency"])]
            await self.forward_to(targets)
        lock = asyncio.Lock()
        rng = random.Random(f"{self.config['seed']}:{self.name}:service")

        async def app(body, headers):
            if not worker:
                return await asyncio.shield(self.admit(body, headers["x-message-id"]))
            # Each worker processes one job at a time and ACKs only after processing.
            metadata = json.loads(body)
            metadata.pop("payload_b64")
            if metadata["id"] != headers["x-message-id"]:
                return 400, b"Message ID mismatch"
            async with lock:
                started = time.monotonic_ns()
                mean = self.config["mean_service_s"]
                delay = rng.expovariate(1 / mean) if self.config["service_mode"] == "exponential" else mean
                await asyncio.sleep(delay)
                processed = time.monotonic_ns()
                self.log("worker", dict(metadata, outcome="processed", worker_id=self.name,
                    service_start_ns=started, processed_ns=processed))
                return 200, json.dumps(dict(id=metadata["id"], outcome="processed", worker_id=self.name,
                    worker_processed_ns=processed, clock_domain_id=self.clock)).encode()

        server = await serve(self.config["protocol"], self.address["ip"], 8443,
                             self.config["certfile"], self.config["keyfile"], app)
        self.ready()
        await self.stop.wait()
        if not worker:
            await self.finish()
        await server.close()
        self.close_logs()


async def robot(node):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node as ROSNode
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import String

    config = node.config
    rclpy.init()
    source = ROSNode("source", namespace="/" + node.name)
    gateway = ROSNode("gateway", namespace="/" + node.name)
    qos = QoSProfile(depth=10000, reliability=ReliabilityPolicy.RELIABLE)
    loop = asyncio.get_running_loop()
    events = schedule(config, node.name)
    kinds = ["probe"] if config["workload"] == "random" else [kind for kind, _, _ in TELEMETRY]
    routes = {kind: kind if config["separate_topics"] else "messages" for kind in kinds}
    publishers = {topic: source.create_publisher(String, topic, qos) for topic in set(routes.values())}
    frames = load_video_frames() if config.get("video_enabled", True) and config["workload"] == "mixed" else []
    video_metadata, generated, observed = {}, set(), set()
    metadata_lock = threading.Lock()

    def accept(body):
        identity = json.loads(body)["id"]
        observed.add(identity)
        node.admit(body)

    def callback(value):
        loop.call_soon_threadsafe(accept, value.data.encode())

    def video_callback(value):
        key = (value.header.stamp.sec, value.header.stamp.nanosec)
        with metadata_lock:
            metadata = video_metadata.pop(key)
        loop.call_soon_threadsafe(accept, message(metadata["id"], "video", bytes(value.data), metadata))

    for topic in publishers:
        gateway.create_subscription(String, topic, callback, qos)
    if frames:
        video = source.create_publisher(CompressedImage, "camera/image/compressed", qos)
        gateway.create_subscription(CompressedImage, "camera/image/compressed", video_callback, qos)
    executor = SingleThreadedExecutor()
    executor.add_node(source)
    executor.add_node(gateway)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    await node.forward_to([("edge" + node.address["site"], config["gateway_concurrency"])])

    # Check actual ROS discovery before telling the runner that this robot is ready.
    deadline = time.monotonic() + 20
    all_publishers = list(publishers.values()) + ([video] if frames else [])
    while any(publisher.get_subscription_count() != 1 for publisher in all_publishers):
        if time.monotonic() > deadline:
            raise RuntimeError("ROS publishers and subscriptions did not discover each other")
        await asyncio.sleep(0.05)
    node.ready(source_node="source", gateway_node="gateway", topics=[p.topic_name for p in all_publishers])
    start_path = Path(config["start_file"])
    while not start_path.exists():
        await asyncio.sleep(0.05)
    timing = json.loads(start_path.read_text())

    # Publish independently of HTTP responses, preserving the original source timestamp.
    for offset, kind, seq, size in events:
        due = timing["start_ns"] + round(offset * 1e9)
        await asyncio.sleep(max(0, (due - time.monotonic_ns()) / 1e9))
        # Loop through the project video when the experiment outlasts the clip.
        payload = frames[seq % len(frames)] if kind == "video" else b"x" * size
        identity = f"{config['run_id']}:{node.name}:{kind}:{seq}"
        metadata = dict(id=identity, robot_id=node.name, topic=kind, payload_bytes=len(payload),
                        source_ns=time.monotonic_ns(), scheduled_ns=due)
        node.log("source", metadata)
        generated.add(identity)
        if kind == "video":
            value = CompressedImage()
            value.header.stamp = source.get_clock().now().to_msg()
            value.header.frame_id, value.format, value.data = node.name + "/camera", "jpeg", payload
            with metadata_lock:
                video_metadata[value.header.stamp.sec, value.header.stamp.nanosec] = metadata
            video.publish(value)
        else:
            publishers[routes[kind]].publish(String(data=message(identity, kind, payload, metadata).decode()))

    # Keep observing the full window and give the last DDS callbacks time to arrive.
    await asyncio.sleep(max(0, (timing["measurement_end_ns"] - time.monotonic_ns()) / 1e9))
    deadline = time.monotonic() + min(2, node.remaining())
    while generated != observed and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    await node.finish()
    executor.shutdown(timeout_sec=3)
    thread.join(timeout=3)
    source.destroy_node()
    gateway.destroy_node()
    rclpy.shutdown()
    node.close_logs(generated=len(generated), observed=len(observed), ros_missing_ids=sorted(generated - observed))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    node = Node(json.loads(Path(args.config).read_text()), args.name, args.output)
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, node.stop.set)
    try:
        if node.address["role"] == "robot":
            await robot(node)
        else:
            await node.run_server()
    except BaseException as exc:
        save_json(node.output / "failure.json", {"error": repr(exc)})
        raise


if __name__ == "__main__":
    asyncio.run(main())
