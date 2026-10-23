"""Isolated E1 veth path, verification, and packet capture (Linux only).

This creates a new network namespace and a uniquely named veth pair. It never
changes the host's primary interface, default route, or loopback qdisc. The HTTP
gateway stays in the original namespace; only the server enters the new one.
All traffic remains on one host: this is an emulated link, not a physical link.
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


class NetworkHarness:
    """Own exactly one fresh namespace/veth pair for one experiment run."""

    def __init__(self, folder, rate_mbit=None, capture_port=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.rate_mbit = None if rate_mbit is None else float(rate_mbit)
        if self.rate_mbit is not None and self.rate_mbit <= 0:
            raise ValueError("rate_mbit must be positive")
        self.token = uuid.uuid4().hex[:10]
        self.namespace = "e1-" + self.token
        self.gateway_interface = "e1g" + self.token
        self.server_interface = "e1s" + self.token
        self.alias = "codex-e1-owned:" + self.token
        self.privilege_prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        self.server_prefix = self.privilege_prefix + ["ip", "netns", "exec", self.namespace]
        self.server_ip = None
        self.gateway_ip = None
        self.subnet = None
        self._namespace_created = False
        self._link_created = False
        self._capture = None
        self._capture_log = None
        self._capture_metadata = None
        self.capture_port = capture_port
        self.command_log = []

    def _run(self, args, *, privileged=False, check=True, namespace=False):
        prefix = self.server_prefix if namespace else self.privilege_prefix if privileged else []
        result = subprocess.run(prefix + list(args), capture_output=True, text=True, timeout=15)
        record = {"argv": prefix + list(args), "returncode": result.returncode,
                  "stdout": result.stdout, "stderr": result.stderr}
        self.command_log.append(record)
        if check and result.returncode:
            raise RuntimeError("Command failed: " + " ".join(record["argv"]) + "\n" + result.stderr)
        return result

    def _json(self, args, **kwargs):
        result = self._run(args, **kwargs)
        if result.returncode:
            return {"error": result.stderr.strip()}
        try:
            return json.loads(result.stdout or "[]")
        except ValueError:
            return {"raw": result.stdout}

    def _unused_subnet(self):
        addresses = self._json(["ip", "-j", "-4", "address", "show"])
        routes = self._json(["ip", "-j", "-4", "route", "show", "table", "all"])
        occupied = []
        for interface in addresses:
            for addr in interface.get("addr_info", []):
                if "local" in addr:
                    occupied.append(ipaddress.ip_network(str(addr["local"]) + "/" + str(addr["prefixlen"]), strict=False))
        for route in routes:
            dst = route.get("dst", "default")
            if dst != "default":
                network = ipaddress.ip_network(dst, strict=False)
                if network.prefixlen:
                    occupied.append(network)
        start = int(self.token[:4], 16) % 256
        for index in range(256):
            candidate = ipaddress.ip_network("10.203." + str((start + index) % 256) + ".0/30")
            if not any(candidate.overlaps(network) for network in occupied):
                return candidate
        raise RuntimeError("No unused 10.203.x.0/30 subnet; refusing to overlap an existing route/address")

    def setup(self):
        for executable in ("ip", "tc", "ping", "python3"):
            if shutil.which(executable) is None:
                raise RuntimeError("Required executable is missing: " + executable)
        self._run(["true"], privileged=True)
        subnet = self._unused_subnet()
        self.subnet = str(subnet)
        self.gateway_ip, self.server_ip = map(str, list(subnet.hosts()))
        # Uniqueness is checked before creation; errors do not adopt old resources.
        if self._run(["ip", "link", "show", "dev", self.gateway_interface], check=False).returncode == 0:
            raise RuntimeError("Generated interface already exists")
        existing = self._run(["ip", "netns", "list"]).stdout.splitlines()
        if any(line.split()[0] == self.namespace for line in existing if line.split()):
            raise RuntimeError("Generated namespace already exists")
        try:
            self._run(["ip", "netns", "add", self.namespace], privileged=True)
            self._namespace_created = True
            self._run(["ip", "link", "add", self.gateway_interface, "type", "veth", "peer", "name", self.server_interface], privileged=True)
            self._link_created = True
            self._run(["ip", "link", "set", "dev", self.gateway_interface, "alias", self.alias], privileged=True)
            self._run(["ip", "link", "set", "dev", self.server_interface, "alias", self.alias], privileged=True)
            self._run(["ip", "link", "set", self.server_interface, "netns", self.namespace], privileged=True)
            self._run(["ip", "address", "add", self.gateway_ip + "/30", "dev", self.gateway_interface], privileged=True)
            self._run(["ip", "address", "add", self.server_ip + "/30", "dev", self.server_interface], namespace=True)
            self._run(["ip", "link", "set", self.gateway_interface, "up"], privileged=True)
            self._run(["ip", "link", "set", self.server_interface, "up"], namespace=True)
            self._run(["ip", "link", "set", "lo", "up"], namespace=True)
            offloads = {}
            if shutil.which("ethtool"):
                for side, device, in_namespace in (("gateway", self.gateway_interface, False), ("server", self.server_interface, True)):
                    result = self._run(["ethtool", "-K", device, "tso", "off", "gso", "off", "gro", "off"], privileged=not in_namespace, namespace=in_namespace, check=False)
                    offloads[side] = {"disable_returncode": result.returncode, "disable_stderr": result.stderr,
                                      "features": self._run(["ethtool", "-k", device], namespace=in_namespace, check=False).stdout}
            else:
                offloads = {"status": "ethtool unavailable; offloads not modified"}
            if self.rate_mbit is not None:
                self._run(["tc", "qdisc", "add", "dev", self.gateway_interface, "root", "handle", "1:", "tbf", "rate", str(self.rate_mbit) + "mbit", "burst", "4000", "latency", "200ms"], privileged=True)
            metadata = {
                "topology": "T0_SHARED_isolated_veth", "physical_link": False,
                "clock_domain": "same Linux kernel monotonic clock",
                "namespace": self.namespace, "gateway_interface": self.gateway_interface,
                "server_interface": self.server_interface, "ownership_alias": self.alias,
                "subnet": self.subnet, "gateway_ip": self.gateway_ip, "server_ip": self.server_ip,
                "server_prefix": self.server_prefix, "network_rate_mbit": self.rate_mbit,
                "shaping_direction": "gateway egress toward server only",
                "response_direction": "unshaped", "injected_delay_ms": 0,
                "injected_random_loss_percent": 0,
                "qdisc": "tbf" if self.rate_mbit else "default veth noqueue",
                "tbf_burst_bytes": 4000 if self.rate_mbit else None,
                "tbf_latency_ms": 200 if self.rate_mbit else None,
                "offloads": offloads,
            }
            _write_json(self.folder / "network_setup.json", metadata)
            self.snapshot("setup")
            return metadata
        except BaseException:
            self.cleanup()
            raise

    def verify(self):
        """Measure unloaded ICMP RTT and bulk TCP goodput before application capture."""
        ping = self._run(["ping", "-n", "-c", "8", "-i", "0.1", "-w", "5", self.server_ip])
        match = re.search(r"(?:rtt|round-trip).*?=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)", ping.stdout)
        if match is None:
            raise RuntimeError("Cannot parse verification ping RTT")
        rtt = dict(zip(("min_ms", "mean_ms", "max_ms", "mdev_ms"), map(float, match.groups())))
        byte_count = 4 * 1024 * 1024 if self.rate_mbit is None else max(65536, int(self.rate_mbit * 1e6 * 3 / 8))
        receiver_code = """import socket,time,json
s=socket.socket();s.bind((__import__('sys').argv[1],0));s.listen(1);s.settimeout(15)
print(s.getsockname()[1],flush=True)
c,_=s.accept();c.settimeout(45);n=0;t0=None;t1=None
while True:
 data=c.recv(65536)
 if not data:break
 now=time.monotonic_ns()
 if t0 is None:t0=now
 n+=len(data);t1=now
elapsed=(t1-t0)/1e9 if t0 is not None and t1>t0 else 0
c.sendall((json.dumps({'received_bytes':n,'receive_seconds':elapsed,'tcp_payload_mbit_s':n*8/elapsed/1e6 if elapsed else None})+'\\n').encode())
c.close();s.close()
"""
        receiver = subprocess.Popen(self.server_prefix + ["python3", "-u", "-c", receiver_code, self.server_ip], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # read a bounded startup line, without a potentially endless readline
            import select
            readable, _, _ = select.select([receiver.stdout], [], [], 10)
            if not readable:
                raise RuntimeError("Verification receiver did not start within 10 seconds")
            line = receiver.stdout.readline().strip()
            if not line.isdigit():
                raise RuntimeError("Verification receiver failed: " + line)
            with socket.create_connection((self.server_ip, int(line)), timeout=10) as connection:
                connection.settimeout(45)
                remaining = byte_count
                block = b"x" * 32768
                while remaining:
                    chunk = block[:min(remaining, len(block))]
                    connection.sendall(chunk)
                    remaining -= len(chunk)
                connection.shutdown(socket.SHUT_WR)
                reply = b""
                while b"\n" not in reply:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
            bandwidth = json.loads(reply)
            receiver.wait(timeout=5)
            if receiver.returncode:
                raise RuntimeError("Verification receiver exit status " + str(receiver.returncode))
        finally:
            if receiver.poll() is None:
                receiver.terminate()
                try:
                    receiver.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    receiver.kill()
                    receiver.wait(timeout=3)
        valid = bandwidth["received_bytes"] == byte_count
        ratio = None
        if self.rate_mbit is not None:
            ratio = bandwidth["tcp_payload_mbit_s"] / self.rate_mbit
            valid = valid and 0.7 <= ratio <= 1.2
        result = {"valid": valid, "rtt": rtt, "ping_stdout": ping.stdout,
                  "bandwidth": bandwidth, "requested_tcp_payload_bytes": byte_count,
                  "configured_egress_rate_mbit": self.rate_mbit,
                  "payload_goodput_to_configured_rate_ratio": ratio,
                  "rate_validity_tolerance": "70%-120% TCP payload goodput/configured rate; lower layers consume capacity",
                  "scope": "pre-run separate TCP bulk upload and unloaded ICMP RTT, excluded from HTTP capture and application results",
                  "bytes_layer": "TCP application payload at verification receiver; not physical wire bytes"}
        _write_json(self.folder / "network_verification.json", result)
        self.snapshot("verified")
        if not valid:
            raise RuntimeError("Network verification failed; inspect network_verification.json")
        return result

    def snapshot(self, tag):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            raise ValueError("Snapshot tag must contain only letters, numbers, underscores or hyphens")
        result = {"monotonic_ns": time.monotonic_ns(),
                  "gateway_qdisc": self._json(["tc", "-j", "-s", "qdisc", "show", "dev", self.gateway_interface], check=False),
                  "server_qdisc": self._json(["tc", "-j", "-s", "qdisc", "show", "dev", self.server_interface], namespace=True, check=False),
                  "gateway_link": self._json(["ip", "-j", "-s", "link", "show", "dev", self.gateway_interface], check=False),
                  "server_link": self._json(["ip", "-j", "-s", "link", "show", "dev", self.server_interface], namespace=True, check=False)}
        _write_json(self.folder / ("network_" + tag + ".json"), result)
        _write_json(self.folder / "network_commands.json", self.command_log)
        return result

    def start_capture(self, port=None):
        port = self.capture_port if port is None else port
        if port is None or not 0 < int(port) < 65536:
            raise ValueError("A valid application TCP port is required")
        if self._capture is not None:
            raise RuntimeError("Capture already started")
        if shutil.which("tcpdump") is None:
            raise RuntimeError("tcpdump is unavailable")
        self._capture_log = (self.folder / "capture_stderr.txt").open("w")
        pcap = self.folder / "capture_gateway.pcap"
        argv = self.privilege_prefix + ["tcpdump", "-Z", "root", "-i", self.gateway_interface, "-nn", "-s", "0", "-B", "8192", "-U", "-w", str(pcap.resolve()), "tcp", "port", str(int(port))]
        self._capture = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=self._capture_log)
        self._capture_metadata = {"argv": argv, "interface": self.gateway_interface,
                                  "namespace": "gateway/default", "direction": "both directions at one interface only",
                                  "filter": "tcp port " + str(int(port)), "snap_length": 0,
                                  "capture_file": pcap.name, "format": "pcap",
                                  "capture_start_monotonic_ns": time.monotonic_ns(),
                                  "physical_link": False,
                                  "offloads": "see network_setup.json",
                                  "capture_drops": None}
        for _ in range(100):
            if self._capture.poll() is not None:
                raise RuntimeError("tcpdump stopped during startup; inspect capture_stderr.txt")
            if pcap.exists() and pcap.stat().st_size >= 24:
                _write_json(self.folder / "capture_metadata.json", self._capture_metadata)
                return self._capture_metadata
            time.sleep(0.02)
        raise RuntimeError("tcpdump did not create its pcap header within two seconds")

    def stop_capture(self):
        if self._capture is None:
            return None
        process = self._capture
        self._capture = None
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        self._capture_log.close()
        log = (self.folder / "capture_stderr.txt").read_text()
        self._capture_metadata.update(capture_end_monotonic_ns=time.monotonic_ns(), exit_code=process.returncode, stderr=log)
        for name, pattern in (("packets_captured", r"(\d+) packets captured"),
                              ("packets_received_by_filter", r"(\d+) packets received by filter"),
                              ("capture_drops", r"(\d+) packets dropped by kernel")):
            match = re.search(pattern, log)
            self._capture_metadata[name] = int(match.group(1)) if match else None
        _write_json(self.folder / "capture_metadata.json", self._capture_metadata)
        return self._capture_metadata

    def cleanup(self):
        """Remove only resources this instance successfully created."""
        errors = []
        try:
            self.stop_capture()
        except Exception as exc:
            errors.append(str(exc))
        if self._link_created:
            link = self._json(["ip", "-j", "link", "show", "dev", self.gateway_interface], check=False)
            if isinstance(link, list) and link and link[0].get("ifalias") == self.alias:
                result = self._run(["ip", "link", "delete", "dev", self.gateway_interface], privileged=True, check=False)
                if result.returncode:
                    errors.append(result.stderr)
                else:
                    self._link_created = False
            else:
                errors.append("Refused interface deletion: ownership alias missing or changed")
        if self._namespace_created and not self._link_created:
            result = self._run(["ip", "netns", "delete", self.namespace], privileged=True, check=False)
            if result.returncode:
                errors.append(result.stderr)
            else:
                self._namespace_created = False
        _write_json(self.folder / "network_cleanup.json", {"success": not errors, "errors": errors,
                    "namespace_removed": not self._namespace_created, "gateway_interface_removed": not self._link_created})
        _write_json(self.folder / "network_commands.json", self.command_log)
        if errors:
            print("E1 network cleanup warnings: " + "; ".join(errors), file=sys.stderr)

    def __enter__(self):
        self.setup()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", required=True,
                        help="Create isolated path, verify it, and clean it up; no ROS/HTTP experiment")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rate-mbit", type=float)
    args = parser.parse_args()
    with NetworkHarness(args.output, args.rate_mbit) as network:
        result = network.verify()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
