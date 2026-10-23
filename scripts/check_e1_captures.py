"""Reconcile all captured HTTP exchanges in a completed E1 sweep."""
import argparse
import hashlib
import json
from pathlib import Path

from e1_capture_check import inspect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.results.glob("*/summary.json")):
        folder = path.parent
        if (folder / "invalid.json").exists():
            continue
        result = inspect(folder, folder / "capture_validation.json")
        rows.append({"run": folder.name, "passed": result["passed"],
                     "requests": result["captured_http_requests"],
                     "responses": result["captured_http_responses"],
                     "capture_drops": result["capture_drops"]})
        print(folder.name, result["captured_http_requests"], "HTTP exchanges verified", flush=True)
    if not rows:
        raise ValueError("No valid completed runs found")
    expected = json.loads((args.results / "validation.json").read_text())
    assert len(rows) == expected["runs_checked"], "Capture and application validation counts differ"
    report = {"all_passed": all(r["passed"] for r in rows), "runs_checked": len(rows),
              "total_http_exchanges": sum(r["requests"] for r in rows), "runs": rows,
              "capture_checker_sha256": hashlib.sha256(Path(__file__).with_name("e1_capture_check.py").read_bytes()).hexdigest()}
    (args.results / "capture_validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "runs"}))


if __name__ == "__main__":
    main()
