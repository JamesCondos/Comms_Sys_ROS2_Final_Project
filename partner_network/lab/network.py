"""Isolated two-site Linux network for the partner ROS 2 / REST laboratory.

Only new, uniquely named namespaces, bridges and veths are modified.  The host
gets no addresses, forwarding changes or routes.  All routing and impairment
take place inside namespaces.  This is a single-host emulation, not a physical
network.  Root is required for live operations; imports and validation are safe
without privileges.  Standard-library only; iproute2 and tcpdump are external
runtime dependencies.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid


SCHEMA_VERSION = 1
PROFILE_DEFAULTS = {"delay_ms": 0.0, "loss_pct": 0.0,
                    "rate_mbit": None, "limit_packets": 1000}
OFFLOAD_FEATURES = ("tcp-segmentation-offload", "generic-segmentation-offload",
                    "generic-receive-offload")


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _number(value, name, minimum, maximum, *, minimum_inclusive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value > maximum or (
            value < minimum if minimum_inclusive else value <= minimum):
        comparison = ">=" if minimum_inclusive else ">"
        raise ValueError(f"{name} must be {comparison} {minimum} and <= {maximum}")
    return value


def validate_profile(profile: dict | None) -> dict:
    """Reject typos and unsupported values before issuing any tc commands."""
    if profile is None:
        profile = {}
    if not isinstance(profile, dict):
        raise ValueError("An impairment profile must be an object")
    unknown = set(profile) - set(PROFILE_DEFAULTS)
    if unknown:
        raise ValueError("Unknown impairment fields: " + ", ".join(sorted(unknown)))
    result = {**PROFILE_DEFAULTS, **profile}
    result["delay_ms"] = _number(result["delay_ms"], "delay_ms", 0, 60000)
    result["loss_pct"] = _number(result["loss_pct"], "loss_pct", 0, 100)
    if result["rate_mbit"] is not None:
        result["rate_mbit"] = _number(result["rate_mbit"], "rate_mbit", 0, 100000,
                                      minimum_inclusive=False)
    limit = result["limit_packets"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000000:
        raise ValueError("limit_packets must be an integer from 1 to 1000000")
    return result


def validate_config(config: dict) -> dict:
    """Validate network fields while allowing application fields in the config."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    robots = config.get("robots")
    if not isinstance(robots, list) or not 1 <= len(robots) <= 16:
        raise ValueError("robots must contain 1 to 16 actual robot definitions")
    ids = []
    for robot in robots:
        if not isinstance(robot, dict) or not re.fullmatch(r"robot[1-9][0-9]*", str(robot.get("id", ""))):
            raise ValueError("Each robot id must have the form robot1, robot2, ...")
        if robot.get("site") not in ("A", "B"):
            raise ValueError("Each robot site must be A or B")
        ids.append(robot["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Robot ids must be unique")
    workers = config.get("workers", 2)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 to 8")
    sites = config.get("sites", {})
    if not isinstance(sites, dict) or set(sites) - {"A", "B"}:
        raise ValueError("sites may contain only A and B")
    normalized_sites = {}
    for site in ("A", "B"):
        definition = sites.get(site, {})
        if not isinstance(definition, dict) or set(definition) - {"forward", "reverse"}:
            raise ValueError(f"Site {site} may contain only forward and reverse profiles")
        normalized_sites[site] = {direction: validate_profile(definition.get(direction))
                                  for direction in ("forward", "reverse")}
    capture = config.get("capture", True)
    if not isinstance(capture, bool):
        raise ValueError("capture must be true or false")
    return {**config, "robots": [dict(robot) for robot in robots], "workers": workers,
            "sites": normalized_sites, "capture": capture}


def verify_installed_profile(profile: dict, qdiscs: list) -> dict:
    """Compare kernel-reported netem options with the requested settings.

    iproute2 JSON represents delay in seconds, loss as a fraction and rate in
    bytes/second. See iproute2 tc/q_netem.c and lib/json_print.c. This checks
    configuration, not empirical RTT, throughput or observed packet loss.
    """
    enabled = profile["delay_ms"] > 0 or profile["loss_pct"] > 0 or profile["rate_mbit"] is not None
    roots = [item for item in qdiscs if item.get("root") or item.get("parent") == "root"]
    if not enabled:
        if any(item.get("kind") != "noqueue" for item in roots):
            raise RuntimeError("A root qdisc remains after clearing the impairment profile")
        return {"matches_requested": True, "enabled": False}
    netems = [item for item in roots if item.get("kind") == "netem" and item.get("handle") == "1:"]
    if len(netems) != 1:
        raise RuntimeError("Requested netem profile was not installed exactly once")
    options = netems[0].get("options", {})
    observed = {"delay_ms": float(options.get("delay", {}).get("delay", 0)) * 1000,
                "loss_pct": float(options.get("loss-random", {}).get("loss", 0)) * 100,
                "rate_mbit": float(options["rate"]["rate"]) * 8 / 1000000 if options.get("rate") else None,
                "limit_packets": options.get("limit")}
    matches = (observed["limit_packets"] == profile["limit_packets"] and
               math.isclose(observed["delay_ms"], profile["delay_ms"], rel_tol=1e-5, abs_tol=0.001) and
               math.isclose(observed["loss_pct"], profile["loss_pct"], rel_tol=1e-5, abs_tol=0.0001) and
               ((observed["rate_mbit"] is None and profile["rate_mbit"] is None) or
                (observed["rate_mbit"] is not None and profile["rate_mbit"] is not None and
                 math.isclose(observed["rate_mbit"], profile["rate_mbit"], rel_tol=1e-5, abs_tol=0.00001))))
    if not matches:
        raise RuntimeError(f"Installed netem options differ from the request: requested={profile}, observed={observed}")
    return {"matches_requested": True, "enabled": True, "observed": observed}


def select_subnets(addresses: list, routes: list, start: int = 0) -> list[str]:
    """Choose three nonoverlapping /24s, excluding all existing IPv4 prefixes."""
    occupied = []
    for interface in addresses:
        for address in interface.get("addr_info", []):
            if address.get("family", "inet") == "inet" and "local" in address:
                occupied.append(ipaddress.ip_network(
                    f"{address['local']}/{address['prefixlen']}", strict=False))
    for route in routes:
        destination = route.get("dst", "default")
        if destination != "default":
            network = ipaddress.ip_network(destination, strict=False)
            if network.version == 4 and network.prefixlen:
                occupied.append(network)
    chosen = []
    for offset in range(256):
        network = ipaddress.ip_network(f"10.220.{(start + offset) % 256}.0/24")
        if not any(network.overlaps(existing) for existing in occupied):
            chosen.append(str(network))
            occupied.append(network)
            if len(chosen) == 3:
                return chosen
    raise RuntimeError("Cannot allocate three unused /24 subnets within 10.220.0.0/16")


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _process_identity(pid: int) -> dict | None:
    """A PID alone is not an ownership token: include boot and process start time."""
    try:
        content = Path(f"/proc/{pid}/stat").read_text()
        fields = content[content.rfind(")") + 2:].split()
        return {"pid": pid, "start_ticks": int(fields[19]), "boot_id": _boot_id()}
    except (FileNotFoundError, ProcessLookupError):
        return None


class LabNetwork:
    """Own one temporary network.  Preserve run_dir/network for reproducibility.

    Use setup(), command(node, argv), start_capture(), stop_capture(), cleanup().
    Applications must be stopped and reaped before cleanup(); unknown processes
    are never killed.  After an interrupted run use LabNetwork.load(run_dir).
    """

    def __init__(self, run_dir: Path, config: dict):
        self.config = validate_config(config)
        self.run_dir = Path(run_dir).resolve()
        self.folder = self.run_dir / "network"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.folder / "resource_journal.json"
        if self.journal_path.exists():
            raise FileExistsError(f"Network journal already exists; use LabNetwork.load(): {self.journal_path}")
        self.token = uuid.uuid4().hex[:8]
        self.alias = "partner-network:" + self.token
        self.nodes = {}
        self.bridges = {}
        self.subnets = {}
        self.profiles = {}
        self.resources = []
        self.captures = {}
        self.offloads = {"ethtool_available": None, "status": "not_checked", "devices": [],
                         "requested_disabled_features": list(OFFLOAD_FEATURES),
                         "note": "Verification covers TSO/GSO/GRO only, not every NIC offload. Virtual captures may differ from physical wire frames."}
        self._capture_processes = {}
        self._capture_files = {}
        self.state = "new"
        self.boot_id = None
        self.created_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @classmethod
    def load(cls, run_dir: Path) -> "LabNetwork":
        """Restore recorded ownership, without creating/adopting any resources."""
        run_dir = Path(run_dir).resolve()
        path = run_dir / "network" / "resource_journal.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION or not re.fullmatch(r"[a-f0-9]{8}", data.get("token", "")):
            raise ValueError("Unknown or invalid network journal schema")
        token = data["token"]
        if data.get("alias") != "partner-network:" + token:
            raise ValueError("Network journal ownership marker is invalid")
        obj = cls.__new__(cls)
        obj.run_dir = run_dir
        obj.folder = path.parent
        obj.journal_path = path
        for name in ("config", "token", "alias", "nodes", "bridges", "subnets", "profiles",
                     "resources", "captures", "state", "boot_id", "created_utc"):
            setattr(obj, name, data[name])
        obj.offloads = data.get("offloads", {"ethtool_available": None, "status": "not_recorded",
                                            "devices": [], "requested_disabled_features": list(OFFLOAD_FEATURES),
                                            "note": "This older journal did not record offload attempts or readback; no disabled-offload claim is made."})
        obj.config = validate_config(obj.config)
        obj._capture_processes, obj._capture_files = {}, {}
        # A journal may be copied elsewhere, but cannot name arbitrary resources.
        for resource in obj.resources:
            name = resource["name"]
            valid = ((resource["kind"] == "namespace" and name.startswith(f"pn-{token}-")) or
                     (resource["kind"] == "link" and name.startswith("p" + token)))
            if not valid or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("Resource name does not belong to the journal token")
        for name, node in obj.nodes.items():
            if node["namespace"] != f"pn-{token}-{name}":
                raise ValueError("Node namespace does not belong to the journal token")
        return obj

    def _save(self):
        data = {name: getattr(self, name) for name in (
            "config", "token", "alias", "nodes", "bridges", "subnets", "profiles",
            "resources", "captures", "offloads", "state", "boot_id", "created_utc")}
        data["schema_version"] = SCHEMA_VERSION
        _write_json(self.journal_path, data)

    def _log(self, record):
        with (self.folder / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False) + "\n")

    def _run(self, args, *, check=True, timeout=15):
        args = [str(value) for value in args]
        started = time.time_ns()
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            self._log({"argv": args, "started_unix_ns": started, "exception": repr(exc)})
            raise
        self._log({"argv": args, "started_unix_ns": started,
                   "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if check and result.returncode:
            raise RuntimeError("Command failed: " + " ".join(args) + "\n" + result.stderr.strip())
        return result

    def _json(self, args):
        result = self._run(args)
        return json.loads(result.stdout or "[]")

    def _require_root(self):
        if not sys.platform.startswith("linux") or not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise RuntimeError("Live network operations require Linux root; run the lab with sudo")
        if self.boot_id is not None and _boot_id() != self.boot_id:
            raise RuntimeError("Journal is from a different Linux boot; refusing to act on possibly reused names")

    @contextmanager
    def _setup_lock(self):
        # Serializes allocation with other copies of this harness, not with E1/E2.
        import fcntl
        with Path("/run/lock/partner-network-setup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _existing_namespace_names(self):
        return [line.split()[0] for line in self._run(["ip", "netns", "list"]).stdout.splitlines()
                if line.split()]

    def _allocate_subnets(self):
        addresses = self._json(["ip", "-j", "-4", "address", "show"])
        routes = self._json(["ip", "-j", "-4", "route", "show", "table", "all"])
        # Existing labs deliberately have no host routes, so inspect named netns too.
        checked = self._existing_namespace_names()
        for namespace in checked:
            prefix = ["ip", "netns", "exec", namespace]
            addresses.extend(self._json(prefix + ["ip", "-j", "-4", "address", "show"]))
            routes.extend(self._json(prefix + ["ip", "-j", "-4", "route", "show", "table", "all"]))
        subnets = select_subnets(addresses, routes, int(self.token[:4], 16) % 256)
        _write_json(self.folder / "allocation_check.json", {
            "checked_namespace_names": checked, "existing_addresses": addresses,
            "existing_routes": routes, "chosen_subnets": subnets})
        return subnets

    def _build_plan(self, subnets):
        self.subnets = dict(zip(("A", "B", "core"), subnets))
        self.bridges = {site: f"p{self.token}b{suffix}"
                        for site, suffix in (("A", "a"), ("B", "b"), ("core", "c"))}
        networks = {site: ipaddress.ip_network(value) for site, value in self.subnets.items()}

        def node(name, role, site, host, *, router=False):
            ip = str(networks[site].network_address + host)
            interfaces = {"lan0" if router else "eth0": {"ip": ip, "subnet": self.subnets[site], "site": site}}
            self.nodes[name] = {"namespace": f"pn-{self.token}-{name}", "ip": ip,
                                "role": role, "site": site, "interfaces": interfaces}
            if router:
                interfaces["wan0"] = {"ip": str(networks["core"].network_address + (1 if site == "A" else 2)),
                                      "subnet": self.subnets["core"], "site": "core"}

        for site in ("A", "B"):
            node("router" + site, "router", site, 1, router=True)
            node("edge" + site, "edge", site, 10)
        site_counts = {"A": 0, "B": 0}
        for robot in self.config["robots"]:
            site = robot["site"]
            node(robot["id"], "robot", site, 20 + site_counts[site])
            site_counts[site] += 1
        node("proxy", "proxy", "core", 10)
        for index in range(self.config["workers"]):
            node(f"worker{index + 1}", "worker", "core", 20 + index)
        index = 0
        for definition in self.nodes.values():
            for interface in definition["interfaces"].values():
                interface["host_veth"] = f"p{self.token}h{index:02x}"
                interface["temporary_peer"] = f"p{self.token}n{index:02x}"
                index += 1

    def command(self, name: str, args: list[str]) -> list[str]:
        """Return argv for a node. No shell expansion or execution is performed."""
        if name not in self.nodes:
            raise KeyError("Unknown network node: " + name)
        if not args or isinstance(args, (str, bytes)):
            raise ValueError("args must be a nonempty argument list")
        return ["ip", "netns", "exec", self.nodes[name]["namespace"], *map(str, args)]

    def _namespace_inode(self, namespace):
        try:
            info = Path("/var/run/netns", namespace).stat()
            return {"device": info.st_dev, "inode": info.st_ino}
        except FileNotFoundError:
            return None

    def _host_link(self, name):
        result = self._run(["ip", "-j", "link", "show", "dev", name], check=False)
        if result.returncode:
            # Distinguish a missing device from an unexpected ip failure.
            if "does not exist" in result.stderr or "Cannot find device" in result.stderr:
                return None
            raise RuntimeError("Cannot inspect link ownership: " + result.stderr.strip())
        values = json.loads(result.stdout)
        return values[0] if len(values) == 1 else None

    def _resource(self, kind, name):
        resource = {"kind": kind, "name": name, "created": False, "marked": False, "removed": False}
        self.resources.append(resource)
        self._save()
        return resource

    def _create_namespace(self, name):
        record = self._resource("namespace", name)
        self._run(["ip", "netns", "add", name])
        record.update(created=True, identity=self._namespace_inode(name))
        if record["identity"] is None:
            raise RuntimeError("Cannot record newly created namespace identity")
        self._save()
        self._run(["ip", "-n", name, "link", "set", "dev", "lo", "alias", self.alias])
        record["marked"] = True
        self._save()
        self._run(["ip", "-n", name, "link", "set", "dev", "lo", "up"])

    def _create_host_link(self, name, creation_args):
        record = self._resource("link", name)
        self._run(creation_args)
        identity = self._host_link(name)
        if not identity:
            raise RuntimeError("Cannot record newly created interface identity")
        record.update(created=True, ifindex=identity["ifindex"])
        self._save()
        self._run(["ip", "link", "set", "dev", name, "alias", self.alias])
        record["marked"] = True
        self._save()
        # Do not create host-side IPv6 link-local addresses when the link goes up.
        self._run(["ip", "link", "set", "dev", name, "addrgenmode", "none"])
        return record

    def _initialize_offloads(self):
        available = shutil.which("ethtool") is not None
        self.offloads.update(ethtool_available=available,
                             status="not_checked" if available else "unavailable")
        self._save()

    def _configure_veth_offloads(self, node_name, host_device, namespace_device):
        """Record both ends; only the three requested features can be verified."""
        for location, device in (("host", host_device), ("namespace", namespace_device)):
            namespace = self.nodes[node_name]["namespace"] if location == "namespace" else None
            row = {"node": node_name, "location": location, "namespace": namespace,
                   "device": device, "set": None, "readback": None,
                   "observed_feature_enabled": {}, "status": "unavailable"}
            if self.offloads["ethtool_available"]:
                def attempt(arguments):
                    argv = self.command(node_name, arguments) if namespace else arguments
                    try:
                        result = self._run(argv, check=False, timeout=5)
                        return {"argv": argv, "returncode": result.returncode,
                                "stdout": result.stdout, "stderr": result.stderr}
                    except Exception as exc:
                        return {"argv": argv, "returncode": None, "exception": repr(exc)}

                row["set"] = attempt(["ethtool", "-K", device, "tso", "off", "gso", "off", "gro", "off"])
                row["readback"] = attempt(["ethtool", "-k", device])
                if row["readback"]["returncode"] == 0:
                    output = row["readback"]["stdout"]
                    for feature in OFFLOAD_FEATURES:
                        match = re.search(r"^\s*" + re.escape(feature) + r":\s*(on|off)(?:\s|$)", output, re.MULTILINE)
                        row["observed_feature_enabled"][feature] = match.group(1) == "on" if match else None
                observed = row["observed_feature_enabled"]
                if all(observed.get(feature) is False for feature in OFFLOAD_FEATURES):
                    row["status"] = "verified"
                elif any(value is True for value in observed.values()) or row["set"]["returncode"] != 0:
                    row["status"] = "not_fully_disabled"
                else:
                    row["status"] = "disabled_unverified"
            self.offloads["devices"].append(row)
        statuses = [row["status"] for row in self.offloads["devices"]]
        self.offloads["status"] = (
            "unavailable" if not self.offloads["ethtool_available"] else
            "verified" if statuses and all(status == "verified" for status in statuses) else
            "not_fully_disabled" if "not_fully_disabled" in statuses else "disabled_unverified")
        self._save()

    def setup(self) -> dict:
        self._require_root()
        if self.state != "new":
            raise RuntimeError("setup() may be called only once on a new lab")
        for executable in ("ip", "tc", "sysctl") + (("tcpdump",) if self.config["capture"] else ()):
            if shutil.which(executable) is None:
                raise RuntimeError("Missing required executable: " + executable)
        self.boot_id = _boot_id()
        self.state = "creating"
        self._save()
        self._initialize_offloads()
        try:
            with self._setup_lock():
                self._build_plan(self._allocate_subnets())
                self._save()
                existing_ns = set(self._existing_namespace_names())
                if any(node["namespace"] in existing_ns for node in self.nodes.values()):
                    raise RuntimeError("Generated namespace already exists; refusing to adopt it")
                names = list(self.bridges.values())
                for node in self.nodes.values():
                    for interface in node["interfaces"].values():
                        names.extend((interface["host_veth"], interface["temporary_peer"]))
                if any(self._host_link(name) is not None for name in names):
                    raise RuntimeError("Generated interface already exists; refusing to adopt it")
                for bridge in self.bridges.values():
                    self._create_host_link(bridge, ["ip", "link", "add", "name", bridge, "type", "bridge"])
                    self._run(["ip", "link", "set", "dev", bridge, "up"])
                for node in self.nodes.values():
                    self._create_namespace(node["namespace"])
                for name, node in self.nodes.items():
                    namespace = node["namespace"]
                    for device, interface in node["interfaces"].items():
                        host, peer = interface["host_veth"], interface["temporary_peer"]
                        self._create_host_link(host, ["ip", "link", "add", host, "type", "veth", "peer", "name", peer])
                        self._run(["ip", "link", "set", "dev", peer, "alias", self.alias])
                        self._run(["ip", "link", "set", "dev", peer, "netns", namespace])
                        self._run(["ip", "-n", namespace, "link", "set", "dev", peer, "name", device])
                        self._run(["ip", "-n", namespace, "link", "set", "dev", device, "addrgenmode", "none"])
                        self._run(["ip", "-n", namespace, "address", "add", interface["ip"] + "/24", "dev", device])
                        self._run(["ip", "-n", namespace, "link", "set", "dev", device, "up"])
                        self._run(["ip", "link", "set", "dev", host, "master", self.bridges[interface["site"]]])
                        self._run(["ip", "link", "set", "dev", host, "up"])
                        self._configure_veth_offloads(name, host, device)
                    for route in self._routes_for(name):
                        self._run(self.command(name, ["ip", "route", "add", *route]))
                    if node["role"] == "router":
                        self._run(self.command(name, ["sysctl", "-q", "-w", "net.ipv4.ip_forward=1"]))
                # Enforce the promised host boundary after constructing all interfaces.
                for bridge in self.bridges.values():
                    records = self._json(["ip", "-j", "address", "show", "dev", bridge])
                    if any(record.get("addr_info") for record in records):
                        raise RuntimeError("An address appeared on a lab host bridge; refusing to continue")
                for site in ("A", "B"):
                    for direction in ("forward", "reverse"):
                        self.apply_profile(site, self.config["sites"][site][direction], direction)
                self.state = "ready"
                self._save()
                topology = self.topology()
                _write_json(self.folder / "topology.json", topology)
                self.snapshot("setup")
                return topology
        except BaseException as original:
            self.state = "setup_failed"
            self._save()
            try:
                self.cleanup()
            except BaseException as cleanup_error:
                self._log({"setup_error": repr(original), "cleanup_error": repr(cleanup_error)})
            raise

    def _routes_for(self, name):
        node = self.nodes[name]
        site = node["site"]
        if site == "core":
            return [[self.subnets[target], "via", self.nodes["router" + target]["interfaces"]["wan0"]["ip"], "dev", "eth0"]
                    for target in ("A", "B")]
        other = "B" if site == "A" else "A"
        if node["role"] == "router":
            return [[self.subnets[other], "via", self.nodes["router" + other]["interfaces"]["wan0"]["ip"], "dev", "wan0"]]
        router_ip = self.nodes["router" + site]["ip"]
        return [[self.subnets[target], "via", router_ip, "dev", "eth0"] for target in ("core", other)]

    def topology(self):
        return {"schema_version": SCHEMA_VERSION, "topology": "two_site_routed_ros2_rest_lab",
                "physical_network": False, "clock_domain": "single Linux kernel",
                "host_ip_addresses_added": False, "host_routes_changed": False,
                "host_forwarding_changed": False, "token": self.token,
                "nodes": self.nodes, "bridges": self.bridges, "subnets": self.subnets,
                "profiles": self.profiles, "capture_interfaces": {"A": "routerA:wan0", "B": "routerB:wan0"},
                "offloads": self.offloads,
                "shaping_direction": {"forward": "site router wan0 egress toward core",
                                      "reverse": "site router lan0 egress toward site"},
                "cross_site_note": "Cross-site traffic traverses the source forward and destination reverse profile.",
                "dds_note": "LAN multicast is local to each site; IP routing does not forward DDS multicast discovery."}

    def _namespace_record(self, namespace):
        for record in self.resources:
            if record["kind"] == "namespace" and record["name"] == namespace and record["created"] and not record["removed"]:
                return record
        raise RuntimeError("Namespace has no active ownership record: " + namespace)

    def _verify_namespace(self, record):
        identity = self._namespace_inode(record["name"])
        if identity is None:
            return False
        if identity != record.get("identity"):
            raise RuntimeError("Namespace identity changed; refusing action: " + record["name"])
        links = self._json(["ip", "-n", record["name"], "-j", "link", "show", "dev", "lo"])
        marker = links[0].get("ifalias") if len(links) == 1 else None
        if record["marked"] and marker != self.alias:
            raise RuntimeError("Namespace ownership marker changed; refusing action: " + record["name"])
        if not record["marked"] and marker not in (None, "", self.alias):
            raise RuntimeError("Unmarked namespace has a foreign marker: " + record["name"])
        return True

    def _verify_link(self, record):
        link = self._host_link(record["name"])
        if link is None:
            return False
        if link.get("ifindex") != record.get("ifindex"):
            raise RuntimeError("Interface identity changed; refusing action: " + record["name"])
        if record["marked"] and link.get("ifalias") != self.alias:
            raise RuntimeError("Interface ownership marker changed; refusing action: " + record["name"])
        if not record["marked"] and link.get("ifalias") not in (None, "", self.alias):
            raise RuntimeError("Unmarked interface has a foreign marker: " + record["name"])
        return True

    def apply_profile(self, site, profile, direction="forward"):
        self._require_root()
        if site not in ("A", "B") or direction not in ("forward", "reverse"):
            raise ValueError("site must be A/B and direction must be forward/reverse")
        profile = validate_profile(profile)
        name, device = "router" + site, "wan0" if direction == "forward" else "lan0"
        record = self._namespace_record(self.nodes[name]["namespace"])
        if not self._verify_namespace(record):
            raise RuntimeError("Router namespace no longer exists")
        links = self._json(self.command(name, ["ip", "-j", "link", "show", "dev", device]))
        if len(links) != 1 or links[0].get("ifalias") != self.alias:
            raise RuntimeError("Router device ownership marker changed")
        qdiscs = self._json(self.command(name, ["tc", "-j", "qdisc", "show", "dev", device]))
        current = [qdisc for qdisc in qdiscs if qdisc.get("root") or qdisc.get("parent") == "root"]
        key = site + ":" + direction
        for qdisc in current:
            if qdisc.get("kind") != "noqueue" and not (
                    key in self.profiles and qdisc.get("kind") == "netem" and qdisc.get("handle") == "1:"):
                raise RuntimeError("Refusing to replace an unowned root qdisc on " + name + ":" + device)
        enabled = profile["delay_ms"] > 0 or profile["loss_pct"] > 0 or profile["rate_mbit"] is not None
        if enabled:
            args = ["tc", "qdisc", "replace", "dev", device, "root", "handle", "1:", "netem",
                    "limit", str(profile["limit_packets"])]
            if profile["delay_ms"]:
                args.extend(("delay", f"{profile['delay_ms']:g}ms"))
            if profile["loss_pct"]:
                args.extend(("loss", "random", f"{profile['loss_pct']:g}%"))
            if profile["rate_mbit"] is not None:
                args.extend(("rate", f"{profile['rate_mbit']:g}mbit"))
            # Record the planned owned handle before mutation, supporting interrupted cleanup.
            previous = self.profiles.get(key)
            self.profiles[key] = {"requested": profile, "enabled": True, "device": device,
                                  "status": "applying", "previous": previous}
            self._save()
            self._run(self.command(name, args))
        elif any(qdisc.get("kind") == "netem" for qdisc in current):
            self._run(self.command(name, ["tc", "qdisc", "del", "dev", device, "root"]))
        observed = self._json(self.command(name, ["tc", "-s", "-j", "qdisc", "show", "dev", device]))
        verification = verify_installed_profile(profile, observed)
        self.profiles[key] = {"requested": profile, "enabled": enabled, "device": device,
                              "status": "applied", "observed_qdisc": observed,
                              "configuration_verification": verification,
                              "applied_monotonic_ns": time.monotonic_ns(),
                              "note": "Kernel-reported options match the request; actual rate/delay/loss still require empirical measurement"}
        self._save()
        _write_json(self.folder / "topology.json", self.topology())
        return self.profiles[key]

    def snapshot(self, tag):
        self._require_root()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            raise ValueError("snapshot tag must contain only letters, numbers, underscore and hyphen")
        target = self.folder / ("snapshot_" + tag + ".json")
        if target.exists():
            raise FileExistsError("Snapshot already exists: " + str(target))
        data = {"monotonic_ns": time.monotonic_ns(), "nodes": {}, "host_bridges": {},
                "offloads": self.offloads}
        for name, node in self.nodes.items():
            if not self._verify_namespace(self._namespace_record(node["namespace"])):
                raise RuntimeError("Cannot snapshot missing namespace " + node["namespace"])
            data["nodes"][name] = {
                "links": self._json(self.command(name, ["ip", "-s", "-j", "link", "show"])),
                "addresses": self._json(self.command(name, ["ip", "-j", "address", "show"])),
                "routes": self._json(self.command(name, ["ip", "-j", "route", "show", "table", "all"])),
                "qdisc": self._json(self.command(name, ["tc", "-s", "-j", "qdisc", "show"]))}
        for site, bridge in self.bridges.items():
            data["host_bridges"][site] = self._json(["ip", "-s", "-j", "link", "show", "dev", bridge])
        _write_json(target, data)
        return data

    def start_capture(self):
        self._require_root()
        if not self.config["capture"]:
            return {"enabled": False}
        if self.captures:
            raise RuntimeError("Captures may be started only once per lab run")
        try:
            for site in ("A", "B"):
                name = "router" + site
                if not self._verify_namespace(self._namespace_record(self.nodes[name]["namespace"])):
                    raise RuntimeError("Capture router namespace no longer exists")
                pcap, log = self.folder / f"site{site}_wan.pcap", self.folder / f"site{site}_tcpdump.log"
                if pcap.exists() or log.exists():
                    raise FileExistsError("Capture files already exist")
                args = self.command(name, ["tcpdump", "-i", "wan0", "-n", "-s", "0", "-U", "--immediate-mode", "-w", str(pcap)])
                stream = log.open("wb")
                self._capture_files[site] = stream
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stream)
                self._capture_processes[site] = process
                identity = _process_identity(process.pid)
                if identity is None:
                    raise RuntimeError("tcpdump exited during startup")
                self.captures[site] = {"argv": args, "identity": identity, "namespace": self.nodes[name]["namespace"],
                                       "pcap": pcap.name, "log": log.name,
                                       "started_monotonic_ns": time.monotonic_ns(), "status": "starting"}
                self._save()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("tcpdump failed: " + log.read_text(errors="replace"))
                    if "listening on" in log.read_text(errors="replace"):
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("tcpdump did not report ready within five seconds")
                self.captures[site]["status"] = "running"
                self.captures[site]["ready_monotonic_ns"] = time.monotonic_ns()
                self._save()
            return self.captures
        except BaseException:
            self.stop_capture()
            raise

    def _capture_identity_matches(self, capture):
        identity = capture["identity"]
        return _process_identity(identity["pid"]) == identity

    def stop_capture(self):
        if not self.captures:
            return {}
        self._require_root()
        active = [(site, capture) for site, capture in self.captures.items() if capture["status"] not in ("stopped", "missing")]
        if active:
            time.sleep(1.0)  # Preserve late ACKs after the application has stopped.
        errors = []
        for site, capture in active:
            pid = capture["identity"]["pid"]
            process = self._capture_processes.get(site)
            if process is not None and process.poll() is not None:
                capture["returncode"] = process.returncode
            elif self._capture_identity_matches(capture):
                os.kill(pid, signal.SIGINT)
                if process is not None:
                    try:
                        capture["returncode"] = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        errors.append(f"Owned tcpdump {pid} did not stop after SIGINT; investigate manually")
                        continue
                else:
                    deadline = time.monotonic() + 5
                    while self._capture_identity_matches(capture) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if self._capture_identity_matches(capture):
                        errors.append(f"Owned tcpdump {pid} remains alive; investigate manually")
                        continue
                    capture["returncode"] = None  # Recovered process was not our child.
            elif _process_identity(pid) is not None:
                # The original capture no longer exists. Never signal its unrelated successor.
                capture["pid_reused_not_signalled"] = True
                capture["returncode"] = None
                capture["exit_status_unavailable"] = True
            else:
                capture["returncode"] = None
                capture["exit_status_unavailable"] = True
            stream = self._capture_files.pop(site, None)
            if stream:
                stream.close()
            logfile = self.folder / capture["log"]
            content = logfile.read_text(errors="replace") if logfile.exists() else ""
            for key, pattern in (("packets_captured", r"(\d+) packets? captured"),
                                 ("packets_received_by_filter", r"(\d+) packets? received by filter"),
                                 ("packets_dropped_by_kernel", r"(\d+) packets? dropped by kernel")):
                match = re.search(pattern, content)
                capture[key] = int(match.group(1)) if match else None
            capture["status"] = "stopped"
            capture["stopped_monotonic_ns"] = time.monotonic_ns()
            capture["statistics_complete"] = all(capture[key] is not None for key in (
                "packets_captured", "packets_received_by_filter", "packets_dropped_by_kernel"))
            capture["capture_complete"] = capture["statistics_complete"] and capture["packets_dropped_by_kernel"] == 0 and capture.get("returncode") in (0, None)
            self._save()
        _write_json(self.folder / "capture_metadata.json", {"sites": self.captures, "errors": errors,
                                                            "offloads": self.offloads})
        if errors:
            raise RuntimeError("; ".join(errors))
        return self.captures

    def cleanup(self):
        """Delete only recorded, identity-checked resources, and never kill apps."""
        self._require_root()
        result = {"started_monotonic_ns": time.monotonic_ns(), "success": False,
                  "removed": [], "already_absent": [], "refusals": []}
        try:
            self.stop_capture()
            active = [record for record in self.resources if record["created"] and not record["removed"]]
            present = []
            # Preflight all ownership and process checks before deleting anything.
            for record in active:
                try:
                    exists = self._verify_namespace(record) if record["kind"] == "namespace" else self._verify_link(record)
                    if not exists:
                        result["already_absent"].append(record["name"])
                        continue
                    present.append(record)
                    if record["kind"] == "namespace":
                        pids = self._run(["ip", "netns", "pids", record["name"]]).stdout.split()
                        if pids:
                            result["refusals"].append({"resource": record["name"], "reason": "Processes still occupy the namespace",
                                                        "pids": [int(pid) for pid in pids],
                                                        "action": "Stop and reap the listed application processes, then retry cleanup. They were not killed."})
                except Exception as exc:
                    result["refusals"].append({"resource": record["name"], "reason": str(exc)})
            owned_links = {record["name"] for record in active if record["kind"] == "link"}
            present_names = {record["name"] for record in present}
            for bridge in self.bridges.values():
                if bridge in present_names:
                    members = self._json(["ip", "-j", "link", "show", "master", bridge])
                    foreign = [link["ifname"] for link in members if link["ifname"] not in owned_links or link.get("ifalias") != self.alias]
                    if foreign:
                        result["refusals"].append({"resource": bridge, "reason": "Foreign interfaces are attached", "interfaces": foreign})
            if result["refusals"]:
                raise RuntimeError("Network cleanup refused; inspect network/cleanup.json and retry after resolving the listed processes or ownership changes")
            # Deleting host veths also removes their peers, then namespaces, then bridges.
            ordered = sorted(present, key=lambda record: (
                2 if record["name"] in self.bridges.values() else 1 if record["kind"] == "namespace" else 0))
            for record in ordered:
                if record["kind"] == "namespace":
                    if self._verify_namespace(record):
                        pids = self._run(["ip", "netns", "pids", record["name"]]).stdout.split()
                        if pids:
                            raise RuntimeError("New processes entered namespace during cleanup; refusing: " + record["name"])
                        self._run(["ip", "netns", "delete", record["name"]])
                elif self._verify_link(record):
                    self._run(["ip", "link", "delete", "dev", record["name"]])
                record["removed"] = True
                result["removed"].append(record["name"])
                self._save()
            for record in active:
                if record["name"] in result["already_absent"]:
                    record["removed"] = True
            remaining = []
            for record in active:
                exists = self._namespace_inode(record["name"]) is not None if record["kind"] == "namespace" else self._host_link(record["name"]) is not None
                if exists:
                    remaining.append(record["name"])
            if remaining:
                raise RuntimeError("Resources remain after cleanup: " + ", ".join(remaining))
            self.state = "cleaned"
            result["success"] = True
            self._save()
        except BaseException as exc:
            result["error"] = repr(exc)
            raise
        finally:
            result["finished_monotonic_ns"] = time.monotonic_ns()
            _write_json(self.folder / "cleanup.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("cleanup", "show"))
    parser.add_argument("run_dir", type=Path, help="Existing run folder containing network/resource_journal.json")
    args = parser.parse_args()
    lab = LabNetwork.load(args.run_dir)
    result = lab.cleanup() if args.action == "cleanup" else lab.topology()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
