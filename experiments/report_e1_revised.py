"""Generate the revised E1 report only from the complete validated final cohort."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics as st

KINDS = ("command", "status", "odometry", "sensor")
COLORS = ("#0072B2", "#E69F00", "#009E73", "#CC79A7")
ROLES = ("reference", "gateway", "server", "network")
LABELS = {"reference": "Reference", "gateway": "Gateway +10 ms",
          "server": "Server +10 ms", "network": "Forward 2 Mbit/s"}
SHORT = {"reference": "Reference", "gateway": "Gateway\n+10 ms",
         "server": "Server\n+10 ms", "network": "Forward\n2 Mbit/s"}
STAGES = ("ros_to_gateway", "queue_wait", "gateway_dispatch",
          "request_to_server", "server_processing", "response_return")
STAGE_LABELS = ("ROS delivery", "FIFO waiting", "Gateway dispatch",
                "Request to server", "Server stage", "Response return")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def get(row, dotted):
    for key in dotted.split("."):
        row = row[key]
    return row


def mean(group, metric):
    return st.mean(get(r["summary"], metric) for r in group)


def tex(value):
    special = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
               "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
               "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(special.get(c, c) for c in str(value))


def f(value, digits=2):
    return f"{value:,.{digits}f}"


def body_label(role):
    """Lowercase sentence labels without changing the SI prefix in Mbit/s."""
    value=LABELS[role]
    return value[0].lower()+value[1:]


def display_utc(value):
    return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(timezone.utc).strftime("%d %B %Y at %H:%M:%S UTC")


def frange(values, digits=2):
    a, b = min(values), max(values)
    return f(a, digits) if a == b else f(a, digits) + "--" + f(b, digits)


def write_csv(path, rows):
    require(bool(rows), f"No rows for {path}")
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def classify(condition):
    values = (condition.get("gateway_dispatch_delay_ms", 0),
              condition.get("server_processing_delay_ms", 0),
              condition.get("network_rate_mbit"))
    mapping = {(0, 0, None): "reference", (10, 0, None): "gateway",
               (0, 10, None): "server", (0, 0, 2): "network"}
    require(values in mapping, f"Unsupported diagnostic configuration: {values}")
    return mapping[values]


def load_validated(results):
    require((results/"validation.json").is_file(),
            "Final root validation.json is required; no partial report is generated")
    validation = read_json(results/"validation.json")
    require(validation.get("all_passed") is True and validation.get("runs_checked") == 35,
            "The final report requires 35 independently validated runs")
    sweep = read_json(results/"sweep_config.json")
    require(sweep["repeats"] == 5 and sorted(sweep["actual_source_counts"]) == [1, 2, 4, 8],
            "Expected five repetitions and actual source counts 1, 2, 4 and 8")
    require((sweep["warmup_s"], sweep["duration_s"], sweep["drain_s"]) == (10, 60, 30),
            "Unexpected final timing; review reporting protocol")
    require(sweep["queue_capacity"] == 1000, "The final protocol requires 1,000 waiting places")
    require(sweep["traffic"] == {
        "command":{"rate_hz":2,"payload_bytes":100}, "status":{"rate_hz":5,"payload_bytes":100},
        "odometry":{"rate_hz":20,"payload_bytes":250}, "sensor":{"rate_hz":5,"payload_bytes":20000}},
        "Traffic differs from the fixed E1 reporting model")
    require(sweep["deadlines_ms"] == {"command":100,"status":200,"odometry":100,"sensor":1000},
            "Deadline targets differ from the fixed E1 reporting model")
    roles = {c["id"]: classify(c) for c in sweep["conditions"]}
    require(sorted(roles.values()) == sorted(ROLES), "Expected exactly one of each diagnostic condition")
    expected = {(role, n, repeat) for role in ROLES
                for n in ([1, 2, 4, 8] if role == "reference" else [8]) for repeat in range(1, 6)}
    planned = [(roles[c], n, r) for c, n, r in read_json(results/"run_order.json")]
    require(len(planned) == 35 and set(planned) == expected, "Saved plan does not match the final matrix")
    checks_by_run = {r["run"]: r for r in validation["runs"]}
    require((results/"capture_validation.json").is_file(), "Final packet-capture reconciliation is required")
    capture_validation=read_json(results/"capture_validation.json")
    require(capture_validation.get("all_passed") is True and capture_validation.get("runs_checked")==35,
            "The report requires 35 reconciled packet captures")
    captures_by_run={r["run"]:r for r in capture_validation["runs"]}
    require(set(captures_by_run)==set(checks_by_run), "Application and packet-validation run IDs differ")
    paths = sorted(results/name/"summary.json" for name in checks_by_run)
    require(len(paths) == 35 and all(path.is_file() for path in paths), "Missing or duplicate validated runs")
    extra = [path for path in results.glob("*/summary.json") if path.parent.name not in checks_by_run]
    for path in extra:
        invalid = path.parent/"invalid.json"
        require(invalid.is_file() and read_json(invalid).get("infrastructure_invalid") is True,
                f"Completed run omitted from final validation without an infrastructure-invalid record: {path.parent.name}")
        require(path.parent.name in validation.get("infrastructure_invalid_runs", []),
                "An excluded attempt is not inventoried by the final validator")
    records, cells = [], {}
    for path in paths:
        folder = path.parent
        summary, config = read_json(path), read_json(folder/"config.json")
        checks = read_json(folder/"validation.json")
        capture=read_json(folder/"capture_validation.json")
        capture_root=captures_by_run[folder.name]
        require(checks.get("passed") is True and checks_by_run.get(folder.name, {}).get("passed") is True,
                f"Unvalidated run: {folder.name}")
        require(capture.get("passed") is True and capture_root.get("passed") is True
                and all(capture["checks"].values()), "A packet capture did not reconcile successfully")
        require(capture["captured_http_requests"] == capture["captured_http_responses"]
                == capture["logged_server_requests"] == checks["processed_all"]
                == capture_root["requests"] == capture_root["responses"],
                "Captured HTTP requests/responses and server/application counts disagree")
        require(capture["capture_drops"] == capture_root["capture_drops"] == 0,
                "Reported packet capture has observation drops")
        require(not (folder/"invalid.json").exists(), f"Run marked invalid: {folder.name}")
        require(summary["schema_version"] == config["schema_version"] == 3, "Unexpected schema")
        role = roles[summary["condition_id"]]
        key = role, summary["actual_sources"], summary["repeat"]
        require(key in expected and key not in cells, f"Unexpected or duplicate cell: {key}")
        require(summary["run"] == folder.name and config["condition_id"] == summary["condition_id"], "Run identity mismatch")
        require(config["repeat"] == key[2] and classify(config) == role,
                "Per-run intervention/repetition disagrees with its condition label")
        require(config["actual_sources"] == config["actual_ros_robots"] == config["robots"] == config["max_ros_robots"] == key[1],
                "Rate-scaled source configuration entered the actual-source dataset")
        require(config["source_rate_multiplier"] == 1 and config["actual_launched_source_nodes"] == key[1],
                "Actual source count or unchanged per-source rate not verified")
        require(config["network_mode"] == "dedicated_veth", "All revised cases must use the isolated veth topology")
        require(config["traffic"] == sweep["traffic"] and config["deadlines_ms"] == sweep["deadlines_ms"], "Traffic changed")
        require((config["warmup_s"],config["duration_s"],config["drain_s"]) == (10,60,30)
                and summary["duration_s"] == 60, "Per-run warm-up, measurement or drain duration changed")
        require(config["queue_capacity"] == 1000 and config["queue_policy"] == "FIFO"
                and config["overflow_policy"] == "drop_tail" and config["protocol"] == "HTTP/1.1"
                and config["max_in_flight"] == config["gateway_count"] == config["server_count"] == 1
                and config["application_arq"] is False, "Per-run architecture differs from the reported baseline")
        canonical = {k: v for k, v in config.items() if k != "config_sha256"}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        require(digest == config["config_sha256"], "Configuration hash mismatch")
        network = read_json(folder/"network_verification.json")
        require(network.get("valid") is True, f"Invalid calibration: {folder.name}")
        require(network["configured_egress_rate_mbit"] == config["network_rate_mbit"],
                "Calibration bandwidth setting disagrees with the application configuration")
        require(config["host_metadata"]["clock_domain_id"] == config["server_host_metadata"]["clock_domain_id"],
                "Host/server timestamp clocks differ")
        for kind in ("all",) + KINDS:
            stats = summary["classes"][kind]
            require(stats["successful"] > 0 and math.isfinite(stats["end_to_end_p95_ms"]), f"No latency samples: {folder.name}/{kind}")
        stats = summary["classes"]["all"]
        stages = {name: stats[name+"_mean_ms"] for name in STAGES}
        require(abs(sum(stages.values()) - stats["end_to_end_mean_ms"]) < 1e-6,
                "The six same-cohort mean stages are not additive")
        record = {"folder": folder, "summary": summary, "config": config,
                  "validation": checks, "network": network, "role": role, "stages": stages, "capture":capture}
        records.append(record)
        cells[key] = record
    require(set(cells) == expected, "The final matrix is incomplete")
    require(sum(r["capture"]["captured_http_requests"] for r in records)==capture_validation["total_http_exchanges"],
            "Root packet-reconciliation total disagrees with run counts")
    groups = {(role, n): [cells[role, n, r] for r in range(1, 6)] for role, n, unused in sorted(expected)}
    return sweep, records, groups, validation


def make_tables(output, records, groups):
    folder = output/"tables"
    folder.mkdir(exist_ok=True)
    individual, summaries, samples, rates, stages = [], [], [], [], []
    metrics = ("offered_rps", "admitted_rps", "completed_rps", "payload_goodput_mbps", "mean_queue_length",
               "sender_busy_percent", "server_handler_busy_percent", "server_processing_busy_percent",
               "gateway_dispatch_busy_percent", "http_busy_percent", "gateway_cpu_percent", "server_cpu_percent",
               "source_cpu_percent", "source_lateness_p95_ms", "source_lateness_max_ms")
    for record in records:
        s = record["summary"]
        row = {"run": s["run"], "condition": record["role"], "actual_sources": s["actual_sources"], "repeat": s["repeat"]}
        row.update({m: s[m] for m in metrics})
        row.update({m: s[m] for m in ("generated", "successful", "queue_drops", "ros_missing", "request_errors", "pending_after_drain")})
        row.update({"all_p95_ms": s["classes"]["all"]["end_to_end_p95_ms"], "all_p99_ms": s["classes"]["all"]["end_to_end_p99_ms"]})
        individual.append(row)
        for value in s["source_rates"]:
            rates.append({"run": s["run"], "condition": record["role"], "actual_sources": s["actual_sources"], **value})
        stages.append({"run": s["run"], "condition": record["role"], "actual_sources": s["actual_sources"],
                       **{name+"_mean_ms": value for name, value in record["stages"].items()}})
    for (role, count), group in groups.items():
        row = {"condition": role, "actual_sources": count, "repeats": len(group)}
        for metric in metrics + ("classes.all.end_to_end_p95_ms", "classes.all.end_to_end_p99_ms"):
            values = [get(r["summary"], metric) for r in group]
            name = metric.replace("classes.all.", "all_")
            row.update({name+"_mean": st.mean(values), name+"_min": min(values), name+"_max": max(values)})
        row["fifo_drop_percent_mean"] = st.mean(100*r["summary"]["queue_drops"]/r["summary"]["generated"] for r in group)
        row["command_deadline_miss_percent_mean"] = 100*mean(group, "deadlines.command.miss_ratio")
        summaries.append(row)
        for kind in KINDS:
            success = [r["summary"]["classes"][kind]["successful"] for r in group]
            generated = [r["summary"]["deadlines"][kind]["generated"] for r in group]
            samples.append({"condition": role, "actual_sources": count, "class": kind,
                "successful_per_run_min": min(success), "successful_per_run_max": max(success),
                "successful_total": sum(success), "generated_per_run_min": min(generated),
                "generated_per_run_max": max(generated), "generated_total": sum(generated),
                "p95_ms_mean": mean(group, "classes."+kind+".end_to_end_p95_ms"),
                "p99_ms_mean": mean(group, "classes."+kind+".end_to_end_p99_ms"),
                "deadline_miss_percent_mean": 100*mean(group, "deadlines."+kind+".miss_ratio")})
    for name, rows in (("individual_runs", individual), ("summary", summaries), ("class_samples", samples),
                       ("source_rates", rates), ("stage_means", stages)):
        write_csv(folder/(name+".csv"), rows)


def make_figures(output, groups, only=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                         "savefig.dpi": 300, "pdf.fonttype": 42, "svg.fonttype": "none"})
    destination = output/"figures"
    destination.mkdir(exist_ok=True)
    names = []

    def save(fig, name):
        require(len(fig.axes) == 1, "Every export must contain one plot")
        if only is not None and name not in only:
            plt.close(fig)
            return
        fig.tight_layout()
        for suffix in ("pdf", "png", "svg"):
            fig.savefig(destination/(name+"."+suffix), bbox_inches="tight")
        plt.close(fig)
        names.append(name)

    def errors(ax, series, x, metric, label, color, marker="o"):
        values = [[get(r["summary"], metric) for r in group] for group in series]
        means = [st.mean(v) for v in values]
        ax.errorbar(x, means, yerr=[[m-min(v) for m,v in zip(means,values)],
                    [max(v)-m for m,v in zip(means,values)]], label=label, color=color,
                    marker=marker, linewidth=1.3, markersize=4, capsize=3)

    counts = [1, 2, 4, 8]
    reference = [groups["reference", n] for n in counts]
    specifications = [
        ("source_throughput", "Completed requests/s", [("completed_rps", "Completed", COLORS[0])]),
        ("source_latency", "Successful-message p95 latency (ms)",
         [("classes."+k+".end_to_end_p95_ms", k, c) for k,c in zip(KINDS,COLORS)]),
        ("source_cpu", "CPU use (% of one logical CPU)",
         [(k+"_cpu_percent", k, c) for k,c in zip(("gateway","server","source"),COLORS)])]
    for name, ylabel, series in specifications:
        fig, ax = plt.subplots(figsize=(5.6, 3.4))
        for i, (metric, label, color) in enumerate(series):
            errors(ax, reference, counts, metric, label, color, ("o","s","^","D")[i])
        if name == "source_throughput":
            ax.plot(counts, [32*n for n in counts], "--", color="0.4", label="Nominal total offered")
        if name == "source_latency":
            ax.set_ylim(0, None)
        ax.set(xlabel="Actual ROS 2 source nodes", ylabel=ylabel)
        ax.set_xticks(counts)
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
        save(fig, name)

    diagnostic = [groups[role,8] for role in ROLES]
    positions = list(range(4))
    for name, ylabel, metric in [
        ("diagnostic_throughput", "Completed requests/s", "completed_rps"),
        ("diagnostic_queue", "Time-averaged waiting messages", "mean_queue_length")]:
        fig, ax = plt.subplots(figsize=(5.6,3.4))
        values = [[get(r["summary"],metric) for r in group] for group in diagnostic]
        means = [st.mean(v) for v in values]
        ax.bar(positions, means, color=COLORS, width=0.62)
        ax.errorbar(positions, means, yerr=[[m-min(v) for m,v in zip(means,values)],
                    [max(v)-m for m,v in zip(means,values)]], fmt="none", color="black", capsize=4)
        if name == "diagnostic_throughput":
            ax.axhline(256,color="0.4",linestyle="--",linewidth=1,label="Nominal total offered: 256/s")
        else:
            ax.axhline(1000,color="0.4",linestyle="--",linewidth=1,label="FIFO capacity: 1,000")
        ax.set_xticks(positions,[SHORT[role] for role in ROLES])
        ax.set(ylabel=ylabel,ylim=(0,None))
        ax.grid(alpha=0.2,axis="y")
        ax.legend(fontsize=8)
        save(fig,name)

    fig, ax = plt.subplots(figsize=(5.6,3.4))
    for kind,color,marker in zip(KINDS,COLORS,("o","s","^","D")):
        errors(ax,diagnostic,positions,"classes."+kind+".end_to_end_p95_ms",kind,color,marker)
    ax.set_xticks(positions,[SHORT[role] for role in ROLES])
    ax.set(ylabel="Successful-message p95 latency (ms)",yscale="log")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.2)
    save(fig,"diagnostic_latency")

    fig, ax = plt.subplots(figsize=(5.6,3.8))
    bottoms = [0.0]*4
    for stage,label,color in zip(STAGES,STAGE_LABELS,("#56B4E9","#0072B2","#E69F00","#009E73","#D55E00","#CC79A7")):
        values = [st.mean(r["stages"][stage] for r in group) for group in diagnostic]
        ax.bar(positions,values,bottom=bottoms,color=color,label=label,width=0.62)
        bottoms = [a+b for a,b in zip(bottoms,values)]
    ax.set_xticks(positions,[SHORT[role] for role in ROLES])
    ax.set(ylabel="Mean successful-message latency (ms)",ylim=(0,None))
    ax.legend(fontsize=8,ncol=2,loc="upper left",bbox_to_anchor=(0,1.25),frameon=False)
    ax.grid(alpha=0.2,axis="y")
    save(fig,"diagnostic_stages")

    fig, ax = plt.subplots(figsize=(5.6,3.4))
    for i,(process,color) in enumerate(zip(("gateway","server","source"),COLORS)):
        values = [[r["summary"][process+"_cpu_percent"] for r in group] for group in diagnostic]
        means = [st.mean(v) for v in values]
        x = [p+(i-1)*0.23 for p in positions]
        ax.bar(x,means,width=0.22,color=color,label=process)
        ax.errorbar(x,means,yerr=[[m-min(v) for m,v in zip(means,values)],
                    [max(v)-m for m,v in zip(means,values)]],fmt="none",color="black",capsize=2)
    ax.set_xticks(positions,[SHORT[role] for role in ROLES])
    ax.set(ylabel="CPU use (% of one logical CPU)",ylim=(0,None))
    ax.legend(fontsize=8)
    ax.grid(alpha=0.2,axis="y")
    save(fig,"diagnostic_cpu")
    return names


def table(headers, rows, caption, label, specification=None):
    spec = specification or "l"+"r"*(len(headers)-1)
    cell = (lambda value: r"{\raggedright "+str(value)+r"\par}") if "p{" in spec else str
    return "\n".join([r"\begin{table}[H]",r"\centering\small",r"\setlength{\tabcolsep}{4pt}",
        r"\begin{tabular}{"+spec+"}",r"\hline"," & ".join(cell(value) for value in headers)+r" \\",r"\hline",
        *[" & ".join(cell(value) for value in row)+r" \\" for row in rows],
        r"\hline",r"\end{tabular}",r"\caption{"+caption+"}",r"\label{"+label+"}",r"\end{table}",""])


def figure(name, caption):
    return "\n".join([r"\begin{figure}[H]",r"\centering",
        r"\includegraphics[width=0.88\linewidth]{figures/"+name+".pdf}",
        r"\caption{"+caption+"}",r"\label{fig:e1r-"+name.replace("_","-")+"}",r"\end{figure}",""])


def build_latex(output, sweep, records, groups, validation, legacy):
    pieces = []
    add = pieces.append
    reference = [groups["reference",n] for n in (1,2,4,8)]
    diagnostic = {role:groups[role,8] for role in ROLES}
    first = records[0]["config"]
    old_summaries=[read_json(p) for p in sorted(legacy.glob("*/summary.json"))]
    require(len(old_summaries)==21 and read_json(legacy/"validation.json")["all_passed"],
            "Historical cohort must contain 21 validated runs")
    old_groups={n:[s for s in old_summaries if s["robots"]==n] for n in (1,2,4,8,16,32,64)}
    require(all(len(g)==3 for g in old_groups.values()),"Historical repetition mismatch")
    def oldmean(n,metric):
        return st.mean(get(s,metric) for s in old_groups[n])
    totals = {key:sum(r["summary"][key] for r in records) for key in
              ("generated","successful","queue_drops","ros_missing","request_errors","pending_after_drain")}
    repeat_text = ("Points or bar heights are means of five run-level statistics; error bars show "
                   "the observed minimum and maximum, not confidence intervals.")
    add(r"\section{E1: Shared-Gateway Baseline and Controlled Bottleneck Diagnosis}")
    add(r"\subsection{Questions and scope}")
    add("This experiment asks whether a shared ROS 2--REST gateway keeps up as the number of actual traffic-source nodes increases, "
        "and how a controlled restriction at the gateway, server or network changes its behaviour. "
        "The architecture has one HTTP client (the gateway), one server and at most one active HTTP/1.1 request. "
        "The TCP connection is reused while healthy; any reconnection is recorded. Increasing ROS 2 source-node count does not increase the number of HTTP clients. "
        "All sources are synthetic; no physical robots or external network links are involved.")
    add("Two cohorts are reported separately. The historical sweep varied total traffic using at most two ROS 2 nodes over loopback. "
        "The revised sweep launches 1, 2, 4 or 8 distinct namespaced ROS 2 source nodes at unchanged per-source rates. "
        "All revised conditions use the same isolated virtual-Ethernet (veth) arrangement, with the HTTP server in a separate Linux network namespace. "
        "This gives a place to apply a directional rate limit while retaining a common host clock. "
        "The revised reference controls the revised diagnostics; it is not a repeat of the historical loopback condition.")
    add(table(["Cohort","ROS 2 sources","Total traffic","Repeats","Timing (s)"],[
        ["Historical loopback","1 or 2","32--2,048/s","3","5 / 60 / 15"],
        ["Revised reference","1, 2, 4, 8","32--256/s","5","10 / 60 / 30"],
        ["Revised diagnostics","8","256/s","5","10 / 60 / 30"]],
        "Distinct E1 cohorts. Timing denotes warm-up / measurement / maximum drain. Traffic rates include all four message classes.",
        "tab:e1r-cohorts"))
    add(r"\subsection{Implementation and experimental procedure}")
    add(r"\subsubsection{Topology and workload}")
    add(r"\begin{center}\fbox{\shortstack{1--8 ROS 2\\source nodes}} $\rightarrow$ "
        r"\fbox{\shortstack{Shared FIFO\\gateway}} $\rightarrow$ "
        r"\fbox{\shortstack{Isolated veth\\path}} $\rightarrow$ "
        r"\fbox{\shortstack{HTTP/1.1\\server}}\end{center}")
    add("The source nodes share one generator process, publish four class-specific topics each, and feed a single gateway process. "
        "The gateway has a FIFO capacity of "+f(sweep["queue_capacity"],0)+" waiting messages, excluding the in-service request; a full queue rejects new arrivals. "
        "The server runs in a separate process. HTTP responses acknowledge the unique message identifier. "
        "The next request is issued only after the previous response has been read and parsed. The request timeout is "+
        f(sweep["request_timeout_s"],0)+" s and application retries are disabled.")
    add(table(["Class","Messages/s/source","Payload bytes","Deadline ms"],
        [[tex(k),f(sweep["traffic"][k]["rate_hz"],0),f(sweep["traffic"][k]["payload_bytes"],0),f(sweep["deadlines_ms"][k],0)] for k in KINDS],
        "Unchanged traffic per actual ROS 2 source node in the revised experiment. Payload excludes metadata and protocol headers. Deadlines are illustrative project targets.",
        "tab:e1r-traffic"))
    add("Each source produces 32 messages/s and 0.8456 Mbit/s of configured application payload. "
        "The four-source and eight-source conditions therefore offer 128 and 256 messages/s; eight sources offer 6.7648 Mbit/s of configured payload. "
        "Each source/class stream is periodic after a seeded initial phase. Messages interleave according to their due times and runtime scheduling; "
        "there is no fixed repeating class order and no random class selection for each publication. "
        "For example, 20 Hz odometry means 50 ms between scheduled publications from one source, not 50 ms of delivery latency; publishing continues while earlier messages wait in the FIFO. "
        "Scheduled and actual publication timestamps make source scheduling lag observable.")
    add(r"\subsubsection{Controlled restrictions and repetitions}")
    add(table(["Condition","Actual sources","Gateway hold","Server hold","Forward rate"],[
        ["Reference","1, 2, 4, 8","0 ms","0 ms","Unshaped"],
        ["Gateway +10 ms","8","10 ms","0 ms","Unshaped"],
        ["Server +10 ms","8","0 ms","10 ms","Unshaped"],
        ["Forward 2 Mbit/s","8","0 ms","0 ms","2 Mbit/s"]],
        "Revised matrix: four reference source counts and three single-restriction diagnostics, each repeated five times (35 runs).",
        "tab:e1r-conditions"))
    add("The gateway hold is a sleep after dequeue and before request transmission; it extends serial gateway service time. "
        "The server hold is a sleep after parsing the request body and before preparing the acknowledgement. It emulates additional service time, not CPU-intensive server work. "
        "The network condition applies a token-bucket rate limit only to gateway egress towards the server. The response direction remains unshaped. "
        "The configured burst is 4,000 bytes and the qdisc latency parameter is 200 ms; the latter bounds shaper buffering and is not a fixed propagation delay. "
        "No random packet loss or propagation delay is intentionally injected. Network queues may still cause delay or drops under load.")
    add("The saved plan shuffles the 35 runs using seed "+str(sweep["seed"])+". Each run warms up for 10 s, measures for 60 s and permits at most 30 s of draining after generation stops. "
        "The same workload runs during warm-up and the FIFO is not reset at the measurement boundary. Under deliberate overload it can therefore already be full when measurement begins. "
        "Source phases use the saved repetition seed. The nominal measurement duration is 2,100 s in total, excluding warm-up, draining, calibration and setup. "
        "Every run saves configuration and code hashes, per-message logs, ROS graph evidence, CPU counters, queue events, network calibration and independent validation. "
        "Repeat numbers identify seeded repetitions, not chronological order.")
    add(r"\subsubsection{Timing boundaries and metrics}")
    add("All latency timestamps use the same verified Linux monotonic clock, including the server in its network namespace. "
        "The endpoint is completion of response-body reading and JSON parsing at the gateway, rather than arrival at the server. "
        "For successful messages generated inside the measurement window, the six contiguous intervals are defined in Table~\\ref{tab:e1r-stages}. "
        "Completions during the bounded drain are included in this latency cohort.")
    add(table(["Interval","Start","Finish"],[
        ["ROS delivery","Before ROS publish","Gateway callback"],
        ["FIFO waiting","Gateway callback","Dequeue"],
        ["Gateway dispatch","Dequeue","Before HTTP request"],
        ["Request to server","Before HTTP request","Body read and parsed"],
        ["Server stage","Body read and parsed","Processing finished"],
        ["Response return","Processing finished","Response read and parsed"]],
        "Additive latency decomposition. Dispatch includes the hold and serialization. Request-to-server and response-return intervals include software work; neither is pure propagation time. The server stage includes its small receive-to-processing-start gap.",
        "tab:e1r-stages","lll"))
    add(r"\[L_{\mathrm{total}}=L_{\mathrm{ROS}}+L_{\mathrm{FIFO}}+L_{\mathrm{dispatch}}"
        r"+L_{\mathrm{request}}+L_{\mathrm{server}}+L_{\mathrm{return}}.\]")
    add("The stacked stage figure adds means from exactly the same successful-message cohort, including the small interval between server receive and processing start. "
        "The reporting code verifies that the six means sum to the end-to-end mean for every run. Percentiles are calculated separately and are never added. "
        "A class p95 is first computed within each run and then averaged across five repetitions; it is not the p95 of pooled messages.")
    add("Offered rate counts messages generated inside the measurement window. Admitted rate counts messages from that source-time cohort that enter the FIFO. "
        "Throughput and payload goodput instead count successful completions inside the response-time window, so a small difference between offered and completed rates can arise at the boundaries. "
        "Payload goodput excludes metadata, HTTP/TCP/IP overhead and responses. A deadline miss includes any generated message that is late or unsuccessful, including FIFO drops, request errors and unresolved work. "
        "Queue occupancy is the time integral of waiting-queue length divided by measurement duration. "
        "CPU percentages use process CPU-time increments over wall time; 100\\% represents one logical CPU. "
        "Cumulative CPU counters are sampled every "+f(sweep["cpu_sample_interval_s"],1)+" s and interpolated at measurement boundaries. "
        "Sender occupancy covers dequeue to response completion; handler occupancy covers handler entry to response writing. "
        "Occupancy includes sleeping or waiting and differs from CPU use. "+repeat_text)

    add(r"\subsection{Actual source-node scaling on the reference path}")
    add("The source-count sweep holds per-source rates and payloads fixed and changes the number of actual ROS 2 nodes. "
        "ROS graph validation confirms the launched nodes and one publisher per expected topic. "
        "Figure~\\ref{fig:e1r-source-throughput} counts completed responses during each 60 s window and compares them with the configured aggregate arrival rate.")
    add(figure("source_throughput","Reference-path throughput versus actual ROS 2 source nodes. The dashed line is 32 messages/s per source. "+repeat_text))
    add("Mean completed rates for 1, 2, 4 and 8 actual sources were "+", ".join(f(mean(g,"completed_rps")) for g in reference)+" requests/s, respectively. "
        "At eight sources, "+f(mean(reference[-1],"offered_rps"))+" messages/s were offered and "+f(mean(reference[-1],"completed_rps"))+" requests/s completed. "
        "Across the 20 reference runs, "+f(sum(r["summary"]["queue_drops"] for g in reference for r in g),0)+" messages were rejected at FIFO admission. "
        "This characterises the tested source counts with the shared HTTP connection held fixed.")
    add("For Figure~\\ref{fig:e1r-source-latency}, successful-message p95 was calculated separately for command, status, odometry and sensor traffic in each run. "
        "All four curves at a given source count experience the same mixed workload. The vertical axis shows latency in milliseconds on a linear scale.")
    add(figure("source_latency","Class-specific p95 latency on the reference path. Each curve represents one class within the same mixed traffic. Successful messages only. "+repeat_text))
    add("At eight sources, mean run-level p95 values were "+", ".join(tex(k)+" "+f(mean(reference[-1],"classes."+k+".end_to_end_p95_ms"))+" ms" for k in KINDS)+". "
        "The mean command deadline-miss percentage was "+f(100*mean(reference[-1],"deadlines.command.miss_ratio"))+r"\%. "
        "Latency describes only successful messages; deadline failures and drops must also be considered before judging timeliness.")
    add("Figure~\\ref{fig:e1r-source-cpu} uses process CPU counters to distinguish source generation, gateway work and server work. "
        "Its x-axis denotes actual ROS 2 nodes, all of which share the source process.")
    add(figure("source_cpu","Process CPU consumption on the reference path, normalised to one logical CPU. "+repeat_text))
    add("At eight sources, source, gateway and server CPU consumption averaged "+
        ", ".join(f(mean(reference[-1],k+"_cpu_percent"))+r"\%" for k in ("source","gateway","server"))+" of one logical CPU, respectively. "
        "These measurements describe demand on this shared host; they do not by themselves establish the maximum sustainable source count.")

    add(r"\clearpage\subsection{Locating the effect of a controlled restriction}")
    add("The following comparisons hold eight actual sources, their mixed traffic and the veth topology fixed. Each diagnostic changes one restriction. "
        "The eight-source reference is therefore the appropriate control for these comparisons.")
    add("Figure~\\ref{fig:e1r-diagnostic-throughput} divides completed requests by the 60 s window. "
        "A 10 ms hold on a serial path alone gives an ideal ceiling of 100 requests/s before other service costs; a 2 Mbit/s limit constrains bytes rather than requests. "
        "Because messages differ in size and admission drops can change the delivered class mix, a byte-rate limit cannot be converted to a request capacity using one fixed message size.")
    add(figure("diagnostic_throughput","Throughput at eight actual sources (nominal aggregate rate 256 messages/s). "+repeat_text))
    add("Mean completed throughput was "+"; ".join(body_label(role)+": "+f(mean(diagnostic[role],"completed_rps"))+" requests/s" for role in ROLES)+". "
        "These values measure the deliberate restrictions under a fixed workload. They are not universal capacities of HTTP/1.1, ROS 2 or the host.")
    diagnostic_rows=[]
    for role in ROLES:
        group=diagnostic[role]
        diagnostic_rows.append([tex(LABELS[role]),f(mean(group,"payload_goodput_mbps")),
            f(st.mean(100*r["summary"]["queue_drops"]/r["summary"]["generated"] for r in group)),
            f(100*mean(group,"deadlines.command.miss_ratio")),f(mean(group,"sender_busy_percent")),f(mean(group,"server_handler_busy_percent"))])
    add(table(["Condition","Mb/s","Drop "+r"\%","Cmd miss "+r"\%","Sender "+r"\%","Handler "+r"\%"],diagnostic_rows,
        "Supporting diagnostic means at eight sources. Mb/s denotes delivered application payload; drops denote FIFO admission losses. Command misses include unsuccessful messages. Activity fractions are wall-time occupancy, not CPU percentages.",
        "tab:e1r-diagnostic-metrics"))
    add("Figure~\\ref{fig:e1r-diagnostic-queue} integrates event-based waiting-queue occupancy. "
        "It measures work waiting before transmission, which packet timing alone cannot recover.")
    add(figure("diagnostic_queue","Mean FIFO occupancy at eight sources, excluding the in-service request. The dashed line marks capacity. "+repeat_text))
    add("Mean queue occupancies were "+"; ".join(body_label(role)+": "+f(mean(diagnostic[role],"mean_queue_length"))+" messages" for role in ROLES)+". "
        "Because warm-up backlog carries into measurement, a nearly full mean queue can describe sustained overload rather than growth from an empty queue. "
        "It indicates that the serial path is not draining arrivals fast enough; this symptom alone does not identify the restricting stage.")
    add("Figure~\\ref{fig:e1r-diagnostic-latency} calculates successful-message p95 separately by class and repetition. "
        "FIFO retains arrival order, so a restriction affecting a long sensor transfer can also delay a later short command.")
    add(figure("diagnostic_latency","Per-class successful-message p95 at eight sources. The vertical axis is logarithmic. "+repeat_text))
    add("Command mean run-level p95 was "+"; ".join(body_label(role)+": "+f(mean(diagnostic[role],"classes.command.end_to_end_p95_ms"))+" ms" for role in ROLES)+". "
        "Queue and deadline measurements distinguish delay among survivors from messages that never completed.")
    reference_records=[record for group in reference for record in group]
    restricted_records=[record for role in ("gateway","server","network") for record in diagnostic[role]]
    if (all(record["summary"]["deadlines"][kind]["miss_ratio"]==0 for record in reference_records for kind in KINDS)
        and all(record["summary"]["deadlines"][kind]["miss_ratio"]==1 for record in restricted_records for kind in KINDS)):
        add("All four classes had zero deadline failures in every one of the 20 reference runs. "
            "Conversely, every class had 100\\% deadline failures in all 15 restricted runs. "
            "Thus the restrictions affected timeliness across the entire FIFO mix, including messages that were eventually delivered successfully.")
    add("Figure~\\ref{fig:e1r-diagnostic-stages} averages each contiguous interval over successful messages in each run, then averages the five run means. "
        "The stack height is the corresponding mean end-to-end latency. When FIFO waiting dominates, small service intervals are hard to see at this scale; "
        "Table~\\ref{tab:e1r-small-stages} retains their numerical values without removing the queue contribution.")
    add(figure("diagnostic_stages","Additive mean latency for successful measurement-cohort messages, including drain completions. Components average five run means. These are not p95 components; no stacked error bars are inferred from component ranges."))
    stage_rows=[]
    for role in ROLES:
        stage_rows.append([tex(LABELS[role])]+[f(st.mean(r["stages"][stage] for r in diagnostic[role]),3) for stage in STAGES[2:]])
    add(table(["Condition","Dispatch ms","Request ms","Server ms","Return ms"],stage_rows,
        "Service-path means from the same successful cohort as the stacked figure. Dispatch includes serialization and any gateway hold; the server stage includes its small pre-processing gap.",
        "tab:e1r-small-stages"))
    for role,stage in (("gateway","gateway_dispatch"),("server","server_processing"),("network","request_to_server")):
        baseline=st.mean(r["stages"][stage] for r in diagnostic["reference"])
        observed=st.mean(r["stages"][stage] for r in diagnostic[role])
        add("For "+body_label(role)+", the "+STAGE_LABELS[STAGES.index(stage)].lower()+" interval averaged "+f(observed,3)+" ms, compared with "+f(baseline,3)+" ms in the reference. "
            "This is a measured service-path component; its upstream waiting consequences are separately visible in the stack.")
    add("Figure~\\ref{fig:e1r-diagnostic-cpu} reports CPU consumption under the same restrictions. "
        "Sleeping for 10 ms or waiting for the network can occupy a service slot without consuming a CPU throughout that interval.")
    add(figure("diagnostic_cpu","Process CPU consumption under controlled restrictions at eight sources. Bars average five runs; error bars show observed minima and maxima. CPU is normalised to one logical CPU."))
    add("With the server hold, server CPU averaged "+f(mean(diagnostic["server"],"server_cpu_percent"))+r"\%, while the handler was occupied for "+
        f(mean(diagnostic["server"],"server_handler_busy_percent"))+r"\% of the window. "
        "The experiment imposes server service time through sleeping and does not demonstrate CPU saturation.")
    evidence_rows=[
        ["Reference path","Eight-source throughput "+f(mean(diagnostic["reference"],"completed_rps"))+"/s; mean queue "+f(mean(diagnostic["reference"],"mean_queue_length"))+".",
         "Tests actual source scaling without an imposed hold or rate cap. It does not establish the natural maximum service capacity."],
        ["Gateway hold","Dispatch "+f(st.mean(r["stages"]["gateway_dispatch"] for r in diagnostic["gateway"]),3)+" ms; sender occupied "+f(mean(diagnostic["gateway"],"sender_busy_percent"))+r"\%.",
         "The hold extends serial service. This is not evidence of gateway CPU exhaustion."],
        ["Server hold","Server stage "+f(st.mean(r["stages"]["server_processing"] for r in diagnostic["server"]),3)+" ms; server CPU "+f(mean(diagnostic["server"],"server_cpu_percent"))+r"\%.",
         "The added service interval is measured. Sleeping does not model CPU-bound processing."],
        ["Forward limit","Bulk calibration "+frange([r["network"]["bandwidth"]["tcp_payload_mbit_s"] for r in diagnostic["network"]])+" Mbit/s.",
         "Forward bandwidth is verified separately. REST goodput also depends on overhead, serial requests and delivered traffic mix."],
        ["Historical overload","At nominal 2,048/s: queue "+f(oldmean(64,"mean_queue_length"))+"; gateway CPU "+f(oldmean(64,"gateway_cpu_percent"))+r"\%.",
         "Consistent overflow and occupied serial sender implicate the client path. Shared-host variability prevents a stable natural-capacity or CPU-causation claim."]]
    add(table(["Intervention","Observed evidence","Interpretation and limit"],evidence_rows,
        "Diagnosis combines the manipulated condition with measured stages. Similar queue and latency symptoms alone do not identify a bottleneck.",
        "tab:e1r-evidence",r"p{0.16\linewidth}p{0.31\linewidth}p{0.43\linewidth}"))
    add(table(["Condition","Sender "+r"\%","Dispatch "+r"\%","Handler "+r"\%","Processing "+r"\%"],
        [[tex(LABELS[role])]+[f(mean(diagnostic[role],metric)) for metric in
          ("sender_busy_percent","gateway_dispatch_busy_percent","server_handler_busy_percent","server_processing_busy_percent")] for role in ROLES],
        "Occupied-time means over five runs. Sender is dequeue to response completion; dispatch is dequeue to request start; handler includes waiting for the request body; processing is the explicit server processing interval. These overlap and must not be added.",
        "tab:e1r-activity"))
    add("Under the forward rate limit, server-handler occupancy averaged "+f(mean(diagnostic["network"],"server_handler_busy_percent"))+
        r"\%, while explicit server-processing occupancy averaged "+f(mean(diagnostic["network"],"server_processing_busy_percent"),4)+r"\%. "
        "The handler starts before the full request body has arrived, so a high handler occupancy can reflect waiting for a shaped upload. "
        "It is not sufficient evidence of a server-processing or CPU bottleneck. The measured request interval, processing interval, CPU and calibrated path must be interpreted together.")

    add(r"\clearpage\subsection{Historical aggregate-load sweep: separate loopback evidence}")
    add("The original 21 runs are retained as evidence about increasing aggregate traffic through a shared gateway. "
        "There was one source node at 32 messages/s and two at the higher tested rates; the two nodes published faster as total offered traffic increased. "
        "Tested totals were 32, 64, 128, 256, 512, 1,024 and 2,048 messages/s. These were generated messages and measured outcomes, not extrapolated points. "
        "They do not represent that many independent clients or a sweep of actual source counts. "
        "These runs used loopback, a 5 s warm-up, 60 s measurement, up to 15 s drain and three repetitions. "
        "Their values are not pooled with the new veth measurements or presented as a before/after optimisation comparison.")
    historical=[
        ("fig4_throughput","Completed throughput",
         "Completed responses inside each measurement window were divided by 60 s. The horizontal axis is the measured total generated rate across all four classes.",
         "At nominal 512 messages/s, mean completed throughput was "+f(oldmean(16,"completed_rps"),1)+" requests/s; at nominal 2,048/s it was "+f(oldmean(64,"completed_rps"),1)+" requests/s. The shared path fell behind the high-load traffic.",
         "Historical loopback throughput versus total offered mixed traffic. Points average three runs; bars show minimum--maximum. The reference line denotes completed rate equal to offered rate."),
        ("fig3_queue","Queue accumulation",
         "Queue occupancy was integrated from event timestamps and excludes the in-service request.",
         "Mean occupancy rose from "+f(oldmean(16,"mean_queue_length"))+" messages at nominal 512/s to "+f(oldmean(64,"mean_queue_length"))+" at nominal 2,048/s, against a capacity of 1,000. This is application backlog, not a router-queue measurement.",
         "Historical loopback mean FIFO occupancy versus total offered mixed traffic. Points average three runs; bars show minimum--maximum."),
        ("fig1_latency","Consequences for latency",
         "Each curve is one class's successful-message p95 under the same combined traffic. The total-rate coordinate is not that class's individual publishing rate.",
         "Mean odometry p95 rose from "+f(oldmean(16,"classes.odometry.end_to_end_p95_ms"))+" ms at nominal 512/s to "+f(oldmean(64,"classes.odometry.end_to_end_p95_ms"))+" ms at nominal 2,048/s. Increased waiting affects all FIFO classes.",
         "Historical loopback per-class source-to-response p95. Points average three run-level p95 values; bars show minimum--maximum. Successful measurement-cohort messages include bounded-drain completions."),
        ("fig2_deadlines","Consequences for deadlines",
         "Deadline-failure fractions include late successful messages and unsuccessful generated messages, including overflow drops. The denominator is all generated messages of the class during measurement.",
         "At nominal 2,048/s, command deadline failures averaged "+f(100*oldmean(64,"deadlines.command.miss_ratio"))+r"\%. Low latency among delivered survivors would therefore be insufficient evidence of acceptable service.",
         "Historical loopback class-specific deadline-failure percentages. Points average three run percentages; bars show minimum--maximum. Targets are shown in the legend."),
        ("fig5_cpu","Resource demand",
         "Process CPU time was measured over the same window; 100 percent denotes one logical CPU.",
         "At nominal 2,048/s, gateway CPU averaged "+f(oldmean(64,"gateway_cpu_percent"),1)+r"\%, compared with "+f(oldmean(64,"server_cpu_percent"),1)+r"\% at the server. The measurements implicate the gateway/client path but do not establish saturation of a physical network link.",
         "Historical loopback process CPU consumption. Points average three runs; bars show minimum--maximum. Multithreaded process CPU can exceed 100 percent."),
        ("fig6_busy_time","Occupied time versus CPU",
         "The sender interval covers dequeue through response parsing; the handler interval covers handler entry through response writing.",
         "At nominal 2,048/s the sender occupied "+f(oldmean(64,"sender_busy_percent"),1)+r"\% of the window, versus "+f(oldmean(64,"server_handler_busy_percent"),1)+r"\% for the server handler. Adding server workers alone cannot be assumed to remove a serial client-side restriction.",
         "Historical loopback sender and server-handler occupied fractions. Points average three runs; bars show minimum--maximum. These wall-time fractions differ from CPU use.")]
    for name,title,construction,interpretation,caption in historical:
        add(r"\subsubsection{"+title+"}")
        add(construction)
        source=legacy/"figures"/(name+".pdf")
        require(source.is_file(),"Missing historical figure "+str(source))
        shutil.copyfile(source,output/"figures"/("historical_"+name+".pdf"))
        add(figure("historical_"+name,caption))
        add(interpretation)
    add("Every historical run at nominal 1,024 and 2,048 messages/s overflowed, but severity varied substantially. "
        "At the highest load, completed throughput ranged from "+frange([s["completed_rps"] for s in old_groups[64]])+
        " requests/s, and successful-message p95 ranged from "+frange([s["classes"]["all"]["end_to_end_p95_ms"] for s in old_groups[64]])+" ms. "
        "Shared-host execution performance changed during the old sweep; the logs do not establish its cause. All repetitions remain included. "
        "Nominal 512/s was the highest tested old load with no overflow in all repetitions; it is an observed reference condition, not a fixed capacity claim.")

    add(r"\clearpage\subsection{Validation, calibration and reproducibility}")
    add("The revised cohort passed independent checks for all "+str(validation["runs_checked"])+" runs. Its measurement windows contain "+
        f(totals["generated"],0)+" generated messages: "+f(totals["successful"],0)+" successful, "+f(totals["queue_drops"],0)+" FIFO drops, "+
        f(totals["ros_missing"],0)+" missing at the gateway, "+f(totals["request_errors"],0)+" request/HTTP errors and "+
        f(totals["pending_after_drain"],0)+" unresolved after draining. These are measurement-cohort counts, excluding warm-up messages. "
        "FIFO drops are valid overload outcomes and do not imply a failed integrity check.")
    warnings=[(r["summary"]["run"],warning) for r in records for warning in r["validation"].get("warnings",[])]
    add("The validator recorded "+str(len(warnings))+" warning entries across these runs. "+
        ("Their exact text is retained in the evidence JSON and per-run validation files." if warnings else "No per-run validation warnings were recorded."))
    excluded=validation.get("infrastructure_invalid_runs",[])
    add("The final validator inventories "+str(len(excluded))+" infrastructure-invalid attempts outside the 35 valid runs. "+
        ("These attempts and their failure records are retained separately; the report does not substitute their partial outcomes for completed runs."
         if excluded else "No infrastructure-invalid attempts were excluded from this final cohort."))
    connections=Counter(r["summary"]["server_connections"] for r in records)
    add("Server-side connection identities show "+", ".join(str(num)+" TCP connection"+("s" if num!=1 else "")+" in "+str(count)+" runs" for num,count in sorted(connections.items()))+". "
        "Checks also cover unique IDs, source sequences and scheduled intervals, topic identities, FIFO order, stage ordering, response agreement, queue capacity, "
        "the one-request concurrency bound, configuration/code hashes, CPU calculations, deadline denominators and message conservation.")
    add("The host exposed "+str(first["host_metadata"]["logical_cpus"])+" logical CPUs. The recorded middleware is \\texttt{"+
        tex(first.get("rmw_implementation","not recorded"))+"}. Run creation spans "+
        display_utc(min(r["config"]["utc_created"] for r in records))+" to "+
        display_utc(max(r["config"]["utc_created"] for r in records))+". "
        "The saved platform string, full-precision ISO timestamps and code/configuration provenance are available in the accompanying evidence file and original configurations.")
    calibration_rows=[]
    for role in ROLES:
        selected=[r for r in records if r["role"]==role]
        calibration_rows.append([tex(LABELS[role]),str(len(selected)),
            frange([r["network"]["rtt"]["mean_ms"] for r in selected],3),
            frange([r["network"]["bandwidth"]["tcp_payload_mbit_s"] for r in selected])])
    add(table(["Condition","Runs","Unloaded RTT (ms)","Bulk payload (Mbit/s)"],calibration_rows,
        "Pre-run path calibration ranges. RTT is the range of run-level unloaded ICMP mean RTTs. Bulk payload goodput comes from a separate TCP upload, not the REST application.",
        "tab:e1r-calibration"))
    add("Each newly created path is verified before application measurement. Rate-limited calibration must deliver 70--120\\% of the configured rate as TCP payload; "
        "the tolerance permits lower-layer overhead and finite-transfer effects. Unshaped calibration is a path sanity check, not a controlled bandwidth target. "
        "Qdisc/interface statistics, offload configuration and setup/cleanup commands are saved. "
        "Successful bulk calibration does not imply that serial mixed HTTP requests attain the same useful payload rate.")
    rate_rows=[]
    for kind in KINDS:
        values=[value["measured_hz"] for r in records for value in r["summary"]["source_rates"] if value["message_type"]==kind]
        rate_rows.append([tex(kind),f(sweep["traffic"][kind]["rate_hz"],0),frange(values,3)])
    add(table(["Class","Configured Hz/source","Measured Hz/source range"],rate_rows,
        "Per-source generation-rate verification over all revised measurement windows. Individual source/run values are supplied in source\\_rates.csv.",
        "tab:e1r-rates"))
    add("Run-level p95 source scheduling lag ranged from "+frange([r["summary"]["source_lateness_p95_ms"] for r in records],3)+
        " ms. The largest individual scheduling lag was "+f(max(r["summary"]["source_lateness_max_ms"] for r in records),3)+" ms. "
        "Rate checks and scheduling lag should be read together: matching the total generated count does not demonstrate perfectly regular spacing.")
    sample_rows=[]
    for role,count in [("reference",1),("reference",2),("reference",4),("reference",8),("gateway",8),("server",8),("network",8)]:
        sample_rows.append([tex(LABELS[role]),str(count)]+[frange([r["summary"]["classes"][kind]["successful"] for r in groups[role,count]],0) for kind in KINDS])
    add(table(["Condition","Sources","Command","Status","Odometry","Sensor"],sample_rows,
        "Successful latency samples per run: observed minimum--maximum across five repetitions. These support the class percentiles and are not pooled sample sizes. Exact generated/successful counts and mean run-level p95/p99 are supplied in class\\_samples.csv.",
        "tab:e1r-samples"))
    add("The main analysis emphasises p95. A single source produces only 120 commands per 60 s run, so its p99 is supported by very few upper-tail observations. "
        "It should not be interpreted as a precise tail guarantee. The sample table makes reduced successful sample counts under overload explicit.")
    captures=[]
    for record in records:
        path=record["folder"]/"capture_metadata.json"
        if path.exists():
            captures.append(read_json(path))
    if captures:
        known=[c["capture_drops"] for c in captures if isinstance(c.get("capture_drops"),int)]
        add(str(len(captures))+" application packet captures were recorded. Kernel capture-drop counts were available for "+
            str(len(known))+" captures and totalled "+f(sum(known),0)+". These describe capture loss, not network loss or retransmissions. "
            "Application logs supply pre-transmission queue and end-to-end measurements; this report does not infer TCP retransmission counts from capture-drop counters.")
        add("Independent TCP stream reassembly reconciled "+f(sum(r["capture"]["captured_http_requests"] for r in records),0)+
            " HTTP requests and the same number of responses with the server logs, including warm-up, measurement and drain exchanges. "
            "The checker verified message identifiers and order, request-body byte totals, a single connection per run and complete captured streams. "
            "These all-run exchange counts have a different time scope from the measurement-cohort totals above.")
    else:
        add("The quantitative conclusions use application and system logs. No application packet-capture count is asserted for the revised cohort.")
    add(r"\subsection{Conclusions and limits}")
    add("The completed evidence separates actual source-node scaling from aggregate rate scaling and distinguishes controlled restrictions through stage timestamps. "
        "The revised source sweep measures 1--8 actual ROS 2 nodes feeding one client. Its diagnostics quantify the effects of gateway waiting, server waiting and forward bandwidth under an unchanged eight-source workload. "
        "The historical high-load sweep supplies complementary evidence of queue growth, deadline failure and repeat variability. "
        "Neither cohort establishes multi-client scalability; independent per-source gateways require a different experiment.")
    add("All processes share one host and kernel. Virtual networking and sleep-based delays isolate mechanisms without reproducing every property of independent machines, "
        "physical radio links or CPU-intensive services. Five repetitions reveal a finite observed spread, not a confidence interval; host scheduling and runtime conditions can still change performance. "
        "The network diagnostic changes forward bandwidth, without independently varying propagation delay or loss. "
        "These results provide a baseline for later scheduling, transport and topology experiments rather than a universal capacity limit or deployment safety claim.")
    section="\n\n".join(pieces)+"\n"
    require("equivalent" not in section.lower(),"Ambiguous historical load label entered the revised report")
    (output/"e1_section.tex").write_text(section,encoding="utf-8")
    build_supplement(output,legacy,old_groups,oldmean)
    reference_group=diagnostic["reference"]
    abstract=("A shared FIFO ROS 2--REST gateway was evaluated in 35 runs using 1--8 actual source nodes and five repetitions per condition. "
        "At eight sources, the reference offered "+f(mean(reference_group,"offered_rps"))+" messages/s and completed "+
        f(mean(reference_group,"completed_rps"))+" requests/s, with a mean run-level successful-message p95 latency of "+
        f(mean(reference_group,"classes.all.end_to_end_p95_ms"))+" ms. At the same offered workload, a 10 ms gateway hold, a 10 ms server hold and a 2 Mbit/s forward-link limit completed "+
        ", ".join(f(mean(diagnostic[role],"completed_rps")) for role in ("gateway","server","network"))+" requests/s, respectively. "
        "Their mean waiting queues were "+", ".join(f(mean(diagnostic[role],"mean_queue_length"),1) for role in ("gateway","server","network"))+
        " messages, compared with "+f(mean(reference_group,"mean_queue_length"),2)+" in the reference. ")
    if all(mean(diagnostic[role],"mean_queue_length")>mean(reference_group,"mean_queue_length") for role in ("gateway","server","network")):
        abstract+=("The restrictions therefore produced upstream application backlog; separate stage timings and CPU measurements distinguish where service time was imposed. ")
    else:
        abstract+=("Separate queue, stage-timing and CPU measurements distinguish the observed effects of the introduced restrictions. ")
    abstract+=("A separate 21-run historical loopback rate sweep is retained without pooling cohorts. "
        "The results quantify this single-client implementation under controlled conditions, rather than a universal natural-capacity limit.")
    (output/"main.tex").write_text(MAIN_TEX.replace("@ABSTRACT@",abstract),encoding="utf-8")
    return totals,warnings


def build_supplement(output,legacy,old_groups,oldmean):
    """The historical supplemental plots retain their original three-run data."""
    parts=[r"\section{Supplementary Historical Loopback Evidence}",
           "This appendix contains the five additional plots from the historical 21-run cohort. "
           "It does not add new measurements or pool the old loopback results with the revised veth experiment. "
           "Historical rate-sweep points average three run-level statistics and error bars show minimum--maximum, not confidence intervals."]
    selected=next(s for s in old_groups[32] if s["repeat"]==1)
    with (legacy/"figures"/"appendix_within_run_variability.csv").open(newline="",encoding="utf-8") as stream:
        bins=list(csv.DictReader(stream))
    require(len(bins)==6 and {r["run"] for r in bins}=={selected["run"]},"Historical variability bins select the wrong run")
    definitions=[
        ("appendix_goodput","Application payload goodput",
         "Payload bytes in successful completions inside the 60 s window were summed, converted to bits and divided by the window duration. "
         "Metadata, protocol headers and acknowledgement responses are excluded. The dashed line denotes configured payload offered across all classes.",
         "Goodput averaged "+f(oldmean(16,"payload_goodput_mbps"))+" Mbit/s at nominal 512 messages/s and "+
         f(oldmean(64,"payload_goodput_mbps"))+" Mbit/s at nominal 2,048 messages/s, despite 54.1184 Mbit/s of configured payload offered at the latter load. "
         "Useful payload and completed request count answer different questions when message sizes and the delivered class mix differ.",
         "Historical loopback application payload goodput. Points average three runs; bars show minimum--maximum. The horizontal rate includes all four classes."),
        ("appendix_drops","Admission loss under overload",
         "Each run's FIFO-drop count was divided by its generated measurement-cohort count; the plot averages those three fractions. "
         "This differs from pooling all messages before computing a fraction, although the reported rounded values are close.",
         "Mean per-run FIFO-drop percentages were "+
         f(st.mean(100*s["queue_drops"]/s["generated"] for s in old_groups[32]))+r"\% at nominal 1,024 messages/s and "+
         f(st.mean(100*s["queue_drops"]/s["generated"] for s in old_groups[64]))+r"\% at nominal 2,048/s. "
         "These are application admission losses. The rejected messages were never sent as HTTP requests.",
         "Historical FIFO admission-drop percentage versus total offered mixed traffic. Points average three run fractions; bars show minimum--maximum."),
        ("appendix_queue_trace","Queue evolution in selected repetitions",
         "The piecewise-constant waiting-queue state was sampled every 100 ms during repeat 1 at nominal 512, 1,024 and 2,048 messages/s. "
         "The same repeat index is selected at every load; this is not a best-run selection. Repeat indices do not indicate chronological run order.",
         "The time traces expose whether backlog is present throughout a measurement or develops within it. "
         "A whole-run mean cannot distinguish those temporal patterns. These three examples should not be assumed to represent every repetition.",
         "Historical waiting-queue traces for repeat 1 of the three highest nominal total traffic rates. Legend values are messages/s across all four classes."),
        ("appendix_within_run_cpu","CPU variation within one run",
         "For repeat 1 at nominal 1,024 messages/s, cumulative CPU counters were interpolated at consecutive 10 s boundaries. "
         "CPU-time differences were divided by elapsed wall time, with 100 percent representing one logical CPU. Lines connect bin averages rather than instantaneous readings.",
         "Gateway CPU changed from "+f(float(bins[0]["gateway_cpu_percent"]),1)+r"\% in the first 10 s bin to "+
         f(float(bins[-1]["gateway_cpu_percent"]),1)+r"\% in the final bin. "
         "This demonstrates changing within-run execution behaviour at a constant configured offered rate; it does not establish a thermal, scheduling or software cause.",
         "Historical process CPU in 10 s bins of repeat 1 at nominal 1,024 total messages/s. No repeat error bars are attached to a single-run trace."),
        ("appendix_within_run_queue","Queue variation within the same run",
         "Queue-state intervals were clipped to the same 10 s boundaries and integrated within each bin. "
         "The six duration-weighted bin means reproduce the validated whole-window occupancy. The dashed line is the 1,000-message waiting capacity.",
         "The first-bin mean queue was "+f(float(bins[0]["mean_queue_length"]))+" messages and the final-bin mean was "+
         f(float(bins[-1]["mean_queue_length"]))+". "
         "Reading this alongside the CPU trace reveals coincident changes in activity and backlog, without identifying a causal explanation.",
         "Historical mean FIFO occupancy in 10 s bins of the same repeat-1 run at nominal 1,024 total messages/s.")]
    for name,title,construction,interpretation,caption in definitions:
        source=legacy/"figures"/(name+".pdf")
        require(source.is_file(),"Missing historical supplemental plot "+str(source))
        shutil.copyfile(source,output/"figures"/("historical_"+name+".pdf"))
        parts.extend([r"\subsection{"+title+"}",construction,figure("historical_"+name,caption),interpretation])
    supplement="\n\n".join(parts)+"\n"
    require("equivalent" not in supplement.lower(),"Ambiguous historical load label entered supplement")
    (output/"e1_supplement.tex").write_text(supplement,encoding="utf-8")


MAIN_TEX = r"""\documentclass[11pt,a4paper]{article}
\usepackage{graphicx}
\usepackage{float}
\setlength{\textwidth}{6.35in}
\setlength{\oddsidemargin}{0.02in}
\setlength{\evensidemargin}{0.02in}
\setlength{\textheight}{9.25in}
\setlength{\topmargin}{-0.55in}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.55em}
\setlength{\emergencystretch}{2em}
\renewcommand{\topfraction}{0.9}
\renewcommand{\bottomfraction}{0.8}
\renewcommand{\textfraction}{0.08}
\title{Shared-Gateway ROS 2--REST Performance:\\Actual Source Scaling and Controlled Bottlenecks}
\author{ELEN90061 Communication Networks}
\date{E1 experimental report}
\begin{document}
\maketitle
\begin{abstract}
@ABSTRACT@
\end{abstract}
\input{e1_section}
\clearpage
\appendix
\input{e1_supplement}
\end{document}
"""

README = """E1 revised report: actual source counts and controlled restrictions

Upload main.tex, e1_section.tex, e1_supplement.tex and figures/ to Overleaf.
Compile main.tex. The optional supplement explains all five extra historical
plots. Omit its input line (and the preceding appendix/clearpage lines) when
space requires the core report alone.
The standalone document uses the standard article class, graphicx and float:
no tau class, booktabs, external bibliography or custom fonts are required.
Overleaf supports both packages. Figures and tables use [H] placement so
production methods, plots and interpretations remain in narrative order.
To reuse it in another report, copy figures/ and input e1_section.tex in a
one-column section or appendix. The tables and inline topology are designed
for a full text width, not a narrow two-column paper column. A two-column
paper needs full-width tables/topology or a deliberate condensed adaptation.
Review section numbering and clear-page boundaries.

Every PDF contains one plot. Eight new figures also have PNG and SVG copies.
All 11 historical PDFs carry a historical_ prefix and unchanged measurements:
six in the core narrative and five in the optional historical appendix.
Historical load axes mean total mixed messages/s. Revised source-count axes
mean actual ROS 2 nodes. Both architectures retain one shared HTTP client.

For a space-limited paper, start with five figures: source_throughput,
source_latency, diagnostic_throughput, diagnostic_queue and diagnostic_stages.
Move CPU figures and historical comparisons/supporting plots to an appendix
as space permits. The complete document is a comprehensive E1 chapter with
all 19 plots, their production methods and interpretations; its full structure
is retained so that you can select material for the final paper.

tables/ contains run results, cell summaries, class sample counts and p95/p99,
all source/class generation rates, and additive mean latency components.
New error bars show the observed min-max of five run-level statistics;
historical figures retain three repetitions. They are not confidence intervals.
report_evidence.json records input hashes, counts and validation warnings.

Regenerate only after all 35 final runs pass independent validation:
python experiments/report_e1_revised.py results/e1_revised_complete --output report/e1_revised

The generator refuses incomplete/unvalidated data. It does not launch traffic
or modify raw results. Generation alone does not assert successful LaTeX
compilation; compile and inspect the rendered pages as a separate check.
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results",type=Path)
    parser.add_argument("--legacy",type=Path,default=Path("results/e1_complete"))
    parser.add_argument("--output",type=Path,default=Path("report/e1_revised"))
    parser.add_argument("--check-only",action="store_true")
    args=parser.parse_args()
    sweep,records,groups,validation=load_validated(args.results)
    if args.check_only:
        print("35 final cells validated for report input; no output files written")
        return
    args.output.mkdir(parents=True,exist_ok=True)
    make_tables(args.output,records,groups)
    names=make_figures(args.output,groups)
    totals,warnings=build_latex(args.output,sweep,records,groups,validation,args.legacy)
    evidence={"schema_version":1,"experiment":"E1","revised_runs":len(records),"historical_runs":21,
        "cohorts_pooled":False,"statistics":"means of run statistics; min-max bars; no pooled percentiles or confidence intervals",
        "measurement_outcomes":totals,"validation_warnings":warnings,"new_figures":names,
        "infrastructure_invalid_runs":validation.get("infrastructure_invalid_runs",[]),
        "inputs":[{"run":r["summary"]["run"],"condition":r["role"],"actual_sources":r["summary"]["actual_sources"],
            "config_sha256":sha256(r["folder"]/"config.json"),"summary_sha256":sha256(r["folder"]/"summary.json"),
            "validation_sha256":sha256(r["folder"]/"validation.json"),
            "capture_validation_sha256":sha256(r["folder"]/"capture_validation.json"),
            "clock_domain_id":r["config"]["host_metadata"]["clock_domain_id"],
            "utc_created":r["config"]["utc_created"],
            "platform":r["config"]["host_metadata"]["platform"],"runner_sha256":r["config"]["code_sha256"],
            "generator_sha256":r["config"]["ros_traffic_sha256"]} for r in records],
        "root_validation_sha256":sha256(args.results/"validation.json"),
        "root_capture_validation_sha256":sha256(args.results/"capture_validation.json"),
        "all_period_http_exchanges":sum(r["capture"]["captured_http_requests"] for r in records),
        "historical_numeric_csv_sha256":{name:sha256(args.legacy/name) for name in
            ("summary_table.csv","class_deadlines.csv","individual_runs.csv")}}
    (args.output/"report_evidence.json").write_text(json.dumps(evidence,indent=2)+"\n",encoding="utf-8")
    (args.output/"README.txt").write_text(README,encoding="utf-8")
    print(f"Wrote {args.output}: 8 new standalone plots, 11 historical plots, 5 data tables and self-contained LaTeX")


if __name__=="__main__":
    main()
