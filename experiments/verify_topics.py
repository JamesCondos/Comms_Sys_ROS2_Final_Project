"""Save real ros2 topic hz output for all eight topics during the stability run."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sweep", type=Path)
    args = parser.parse_args()
    until = time.monotonic() + 40
    while not list(args.sweep.glob("*/topics.json")):
        if time.monotonic() > until:
            raise RuntimeError("No topic manifest appeared")
        time.sleep(0.2)
    specs = json.loads(next(args.sweep.glob("*/topics.json")).read_text())
    output, checks = [], []
    for spec in specs:
        command = ["timeout", "8", "ros2", "topic", "hz", spec["topic"], "--window", "20"]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONUNBUFFERED="1"))
        output += ["$ " + " ".join(command), f"Expected rate: {spec['rate_hz']} Hz", result.stdout]
        rates = re.findall(r"average rate:\s*([0-9.]+)", result.stdout)
        observed = float(rates[-1]) if rates else None
        checks.append(dict(spec, observed_hz=observed,
                           passed=observed is not None and abs(observed/spec["rate_hz"] - 1) < 0.1))
    (args.sweep / "topic_hz.txt").write_text("\n".join(output))
    (args.sweep / "topic_verification.json").write_text(json.dumps(checks, indent=2) + "\n")
    assert all(s["passed"] for s in checks), "A topic did not match its configured rate"
    print(f"All {len(checks)} ROS2 topics matched configured rates within 10%.", flush=True)


if __name__ == "__main__":
    main()
