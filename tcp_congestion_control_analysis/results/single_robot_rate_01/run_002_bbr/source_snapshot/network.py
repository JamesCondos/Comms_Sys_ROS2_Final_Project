"""Create two routed robot sites connected to a central proxy/workers network."""

import ipaddress
import json
import math
import subprocess
import os
from pathlib import Path
import re
import shutil
import signal
import time

from .metrics import save_json
import uuid


class LabNetwork:
    def __init__(self, config, output):
        self.config = config
        self.token = uuid.uuid4().hex[:8]
        self.nodes, self.bridges = {}, {}
        self.namespaces = []
        self.output = Path(output)
        self.created_links = []
        self.namespace_inodes = {}
        self.captures = []
        self.alias = "simple-testbed:" + self.token

    def run(self, args, check=True):
        # Stop immediately when setup or shaping fails instead of testing the wrong network.
        result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=15)
        if check and result.returncode:
            raise RuntimeError("Command failed: " + " ".join(map(str, args)) + "\n" + result.stderr)
        return result

    def save_resources(self):
        # This journal also lets the cleanup command recover an interrupted run.
        save_json(self.output / "resources.json", dict(token=self.token, alias=self.alias,
            nodes=self.nodes, bridges=self.bridges, created_links=self.created_links,
            namespaces=self.namespaces, namespace_inodes=self.namespace_inodes))

    def command(self, name, args):
        """Run a command inside a node's network namespace."""
        return ["ip", "netns", "exec", self.nodes[name]["namespace"], *args]

    def read_json(self, args):
        return json.loads(self.run(args).stdout)

    def choose_subnets(self):
        """Choose three unused /24 networks for site A, B and the core."""
        occupied = []
        locations = [[]]

        for line in self.run(["ip", "netns", "list"]).stdout.splitlines():
            locations.append(["ip", "netns", "exec", line.split()[0]])

        for prefix in locations:
            for interface in self.read_json(prefix + ["ip", "-j", "-4", "address"]):
                for addr in interface.get("addr_info", []):
                    occupied.append(ipaddress.ip_network(
                        f"{addr['local']}/{addr['prefixlen']}", strict=False
                    ))

            for route in self.read_json(prefix + ["ip", "-j", "-4", "route"]):
                if route.get("dst", "default") != "default":
                    occupied.append(ipaddress.ip_network(route["dst"], strict=False))

        chosen = []
        start = int(self.token[:4], 16) % 256

        for i in range(256):
            net = ipaddress.ip_network(f"10.220.{(start + i) % 256}.0/24")
            if not any(net.overlaps(x) for x in occupied):
                chosen.append(net)
            if len(chosen) == 3:
                break

        if len(chosen) != 3:
            raise RuntimeError("Three unused site/core subnets are required")
        return dict(zip(("A", "B", "core"), chosen))

    def add_node(self, name, role, site, host):
        """Store the addressing and interfaces for one node."""
        ip = str(self.subnets[site].network_address + host)
        device = "lan0" if role == "router" else "eth0"
        interfaces = {device: {"ip": ip, "site": site}}

        if role == "router":
            interfaces["wan0"] = {
                "ip": str(self.subnets["core"].network_address +
                          (1 if site == "A" else 2)),
                "site": "core"
            }

        self.nodes[name] = {
            "namespace": f"th-{self.token}-{name}",
            "ip": ip,
            "role": role,
            "site": site,
            "interfaces": interfaces
        }

    def setup(self):
        """Create nodes, bridges, namespaces, interfaces and routes."""
        self.subnets = self.choose_subnets()
        self.bridges = {
            site: f"t{self.token}b{i}"
            for i, site in enumerate(("A", "B", "core"))
        }

        # Create logical nodes.
        for site in ("A", "B"):
            self.add_node("router" + site, "router", site, 1)
            self.add_node("edge" + site, "edge", site, 10)

        # Create only the robots requested by this experiment.
        for robot in self.config["robots"]:
            number = int(robot["id"].removeprefix("robot")) - 1
            self.add_node(robot["id"], "robot", robot["site"], 20 + number % 2)

        self.add_node("proxy", "proxy", "core", 10)
        self.add_node("worker1", "worker", "core", 20)
        self.add_node("worker2", "worker", "core", 21)

        # Create site/core Ethernet bridges.
        for bridge in self.bridges.values():
            self.run(["ip", "link", "add", bridge, "type", "bridge"])
            self.created_links.append(bridge)
            self.run(["ip", "link", "set", bridge, "alias", self.alias])
            self.save_resources()
            self.run(["ip", "link", "set", bridge, "up"])

        number = 0

        # Create each network namespace and attach its interfaces.
        for name, node in self.nodes.items():
            ns = node["namespace"]
            self.run(["ip", "netns", "add", ns])
            self.namespaces.append(ns)
            self.namespace_inodes[ns] = Path("/var/run/netns", ns).stat().st_ino
            self.save_resources()
            self.run(["ip", "-n", ns, "link", "set", "lo", "up"])

            for device, interface in node["interfaces"].items():
                host = f"t{self.token}h{number:02x}"
                peer = f"t{self.token}n{number:02x}"
                number += 1

                # Create veth pair between namespace and bridge.
                self.run(["ip", "link", "add", host, "type", "veth",
                          "peer", "name", peer])
                self.created_links.append(host)
                self.run(["ip", "link", "set", host, "alias", self.alias])
                self.save_resources()
                self.run(["ip", "link", "set", peer, "netns", ns])
                self.run(["ip", "-n", ns, "link", "set", peer, "name", device])
                self.run(["ip", "-n", ns, "address", "add",
                          interface["ip"] + "/24", "dev", device])
                self.run(["ip", "-n", ns, "link", "set", device, "up"])
                self.run(["ip", "link", "set", host, "master",
                          self.bridges[interface["site"]]])
                self.run(["ip", "link", "set", host, "up"])

                # Disable packet offloading so traffic shaping is accurate.
                off = ["tso", "off", "gso", "off", "gro", "off"]
                if shutil.which("ethtool"):
                    self.run(["ethtool", "-K", host, *off])
                    self.run(self.command(name, ["ethtool", "-K", device, *off]))
                # Missing ethtool is recorded rather than pretending offloads were disabled.

            for route in self.routes_for(name):
                self.run(self.command(name, ["ip", "route", "add", *route]))

            if node["role"] == "router":
                self.run(self.command(name, [
                    "sysctl", "-q", "-w", "net.ipv4.ip_forward=1"
                ]))

        # Apply delay/loss/bandwidth limits.
        for site in ("A", "B"):
            for direction in ("forward", "reverse"):
                self.apply_profile(
                    site, direction, self.config["sites"][site][direction]
                )

    def routes_for(self, name):
        """Return the routes required by a node."""
        node = self.nodes[name]
        site = node["site"]

        if site == "core":
            return [
                [str(self.subnets[target]), "via",
                 self.nodes["router" + target]["interfaces"]["wan0"]["ip"],
                 "dev", "eth0"]
                for target in ("A", "B")
            ]

        other = "B" if site == "A" else "A"

        if node["role"] == "router":
            gateway = self.nodes["router" + other]["interfaces"]["wan0"]["ip"]
            return [[str(self.subnets[other]), "via", gateway, "dev", "wan0"]]

        gateway = self.nodes["router" + site]["ip"]
        return [
            [str(self.subnets[target]), "via", gateway, "dev", "eth0"]
            for target in ("core", other)
        ]

    def apply_profile(self, site, direction, profile):
        """Apply delay, packet loss and rate limits to a router."""
        
        device = "wan0" if direction == "forward" else "lan0"

        delay = profile.get("delay_ms", 0)
        loss = profile.get("loss_pct", 0)
        rate = profile.get("rate_mbit")

        if not (delay or loss or rate):
            self.run(self.command("router" + site,
                ["tc", "qdisc", "del", "dev", device, "root"]), check=False)
            self.check_profile(site, device, profile)
            return

        args = [
            "tc", "qdisc", "replace", "dev", device,
            "root", "netem", "limit", "1000"
        ]

        if delay:
            args += ["delay", f"{delay:g}ms"]
        if loss:
            args += ["loss", f"{loss:g}%"]
        if rate:
            args += ["rate", f"{rate:g}mbit"]

        self.run(self.command("router" + site, args))
        self.check_profile(site, device, profile)



    def check_profile(self, site, device, profile):
        # Read back the kernel settings so a failed clear/change cannot go unnoticed.
        rows = self.read_json(self.command("router" + site, ["tc", "-j", "qdisc", "show", "dev", device]))
        shaped = [row for row in rows if row["kind"] == "netem"]
        enabled = profile.get("delay_ms") or profile.get("loss_pct") or profile.get("rate_mbit")
        if not enabled:
            if shaped:
                raise RuntimeError("The old network impairment was not removed")
            return
        if len(shaped) != 1:
            raise RuntimeError("The requested network impairment was not installed")
        options = shaped[0]["options"]
        actual = dict(delay_ms=options.get("delay", {}).get("delay", 0) * 1000,
                      loss_pct=options.get("loss-random", {}).get("loss", 0) * 100,
                      rate_mbit=options["rate"]["rate"] * 8 / 1e6 if options.get("rate") else None)
        for field, value in actual.items():
            expected = profile.get(field, None if field == "rate_mbit" else 0)
            if value is None or expected is None:
                matches = value is expected
            else:
                matches = math.isclose(value, expected, rel_tol=1e-5, abs_tol=1e-4)
            if not matches:
                raise RuntimeError("Installed network " + field + " differs from the requested setting")

    def snapshot(self, name):
        # Retain the installed qdiscs and counters, not just the requested numbers.
        state = {}
        for site in ("A", "B"):
            state[site] = self.read_json(self.command("router" + site,
                ["tc", "-j", "-s", "qdisc", "show"]))
        save_json(self.output / ("snapshot_" + name + ".json"), state)

    def start_capture(self):
        for site in ("A", "B"):
            path = self.output / ("site" + site + "_wan.pcap")
            log = self.output / ("site" + site + "_tcpdump.log")
            stream = log.open("w")
            args = self.command("router" + site, ["tcpdump", "-i", "wan0", "-n",
                "-s", "0", "-U", "--immediate-mode", "-w", str(path), "port", "8443"])
            process = subprocess.Popen(args, stdout=stream, stderr=stream)
            self.captures.append((site, process, stream, log))
            capture_records = []
            for capture_site, capture_process, _, _ in self.captures:
                ticks = Path(f"/proc/{capture_process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                capture_records.append(dict(pid=capture_process.pid, start_ticks=ticks,
                    boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    pcap=str(self.output / ("site" + capture_site + "_wan.pcap"))))
            save_json(self.output / "capture_processes.json", capture_records)
            deadline = time.monotonic() + 5
            while "listening on" not in log.read_text():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Packet capture did not start: " + str(log))
                time.sleep(0.05)

    def stop_capture(self):
        # Give final packets time to reach tcpdump before asking it to flush and stop.
        time.sleep(0.3)
        metadata = {"sites": {}, "errors": []}
        for site, process, stream, log in self.captures:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                metadata["errors"].append("Capture required a forced stop: " + site)
            stream.close()
            text = log.read_text()
            counts = {}
            for field, label in (("packets_captured", "captured"),
                                 ("packets_dropped_by_kernel", "dropped by kernel")):
                match = re.search(r"(\d+) packets " + label, text)
                counts[field] = int(match.group(1)) if match else None
            complete = all(value is not None for value in counts.values()) and process.returncode == 0
            metadata["sites"][site] = dict(counts, capture_complete=complete, statistics_complete=complete)
        save_json(self.output / "capture_metadata.json", metadata)

    def cleanup(self):
        # Delete only namespaces and labelled interfaces created by this exact run.
        errors = []
        for ns in reversed(self.namespaces):
            path = Path("/var/run/netns", ns)
            if not path.exists():
                continue
            if path.stat().st_ino != self.namespace_inodes[ns]:
                errors.append("Namespace ownership changed: " + ns)
                continue
            if self.run(["ip", "netns", "pids", ns]).stdout.strip():
                errors.append("Namespace still contains processes: " + ns)
                continue
            self.run(["ip", "netns", "delete", ns])
        if errors:
            save_json(self.output / "cleanup.json", {"success": False, "errors": errors})
            raise RuntimeError("Cleanup needs attention: " + "; ".join(errors))
        for name in reversed(self.created_links):
            result = self.run(["ip", "-j", "link", "show", "dev", name], check=False)
            if result.returncode:
                continue
            if json.loads(result.stdout)[0].get("ifalias") != self.alias:
                errors.append("Interface ownership changed: " + name)
                continue
            self.run(["ip", "link", "delete", name])
        save_json(self.output / "cleanup.json", {"success": not errors, "errors": errors})
        if errors:
            raise RuntimeError("Cleanup needs attention: " + "; ".join(errors))

    @classmethod
    def load(cls, output):
        # Reload only the recorded resources; this never searches by a broad name pattern.
        network = cls({}, output)
        saved = json.loads((Path(output) / "resources.json").read_text())
        for name, value in saved.items():
            setattr(network, name, value)
        return network

    def recover_captures(self):
        # A hard-killed runner may leave tcpdump alive. Check its identity before stopping it.
        path = self.output / "capture_processes.json"
        if not path.exists():
            return
        for row in json.loads(path.read_text()):
            stat = Path(f"/proc/{row['pid']}/stat")
            if not stat.exists() or row["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                continue
            ticks = stat.read_text().rsplit(")", 1)[1].split()[19]
            command = Path(f"/proc/{row['pid']}/cmdline").read_bytes()
            if ticks == row["start_ticks"] and b"tcpdump" in command and row["pcap"].encode() in command:
                try:
                    os.kill(row["pid"], signal.SIGINT)
                except ProcessLookupError:
                    pass
        time.sleep(.3)
