# Reproducing E2

Read `E2_PROTOCOL.md` for boundaries, experimental controls and the documented
E9 ordering deviation. E2A is model calibration; E2B measures live ROS 2/HTTP.
Use a new output directory for each execution and retain unsuccessful attempts.

## Controlled model

Python and NumPy are required. The retained formal model dataset was generated
on Windows, separately from all live timing measurements.

```powershell
python -B experiments/e2_model.py --config experiments/configs/e2_model_complete.json --output results/e2_model_new_run
python -B experiments/validate_e2_model.py results/e2_model_new_run
python -B scripts/e1_results_manifest.py results/e2_model_new_run
python -B scripts/e1_results_manifest.py results/e2_model_new_run --verify
```

The manifest helper's historical filename does not restrict it to E1. It records
relative file paths and SHA-256 hashes and refuses to replace an existing manifest.
Model `jobs.npz` and `events.npz` files contain named NumPy arrays in simulated
seconds. Load with `numpy.load(path, allow_pickle=False)`. Event kind 0 is a
departure, 1 an admitted arrival and 2 a rejection. Occupancy metrics integrate
the full event trace; `samples.csv` is a readable illustrative time series.

## Live ROS 2/HTTP runs

Use Ubuntu 22.04, ROS 2 Humble, Python 3, `rclpy`, `std_msgs`, `iproute2`, `ping`
and `tcpdump`. Root or equivalent network and capture capabilities are required.
The harness only modifies its uniquely named virtual interfaces and namespace.
The wrapper selects ROS domain 163 and loads source code from this checkout.

```bash
sudo bash scripts/run_e2_live.sh --config experiments/configs/e2_live_pilot.json --output results/e2_live_new_pilot
sudo bash scripts/run_e2_live.sh --config experiments/configs/e2_live_complete.json --output results/e2_live_new_run
python3 -B experiments/validate_e2_live.py results/e2_live_new_run
python3 -B scripts/check_e1_captures.py results/e2_live_new_run
python3 -B scripts/e1_results_manifest.py results/e2_live_new_run
python3 -B scripts/e1_results_manifest.py results/e2_live_new_run --verify
```

The capture checker is shared with E1 because both runners retain compatible
message IDs, HTTP fields and capture metadata. It reconstructs HTTP exchanges
across TCP segments and compares all-period identities and bytes with server
records. Its counts include warm-up and drain and therefore differ from the
measurement-window cohort.

Formal live runs need roughly 50 minutes. Avoid concurrent model execution,
plotting, compilation or other intensive work on the host. Pilot data are
excluded from formal statistics. Keep the exact final configuration and source
snapshots with the raw measurements.

On this workstation a separate Linux execution directory is used:
`/home/jcondos/comm_nets_e2_20261023`. The Google Drive G: mount is not directly
available in WSL. Stage code and returned results through a Windows temporary
directory on C:, then verify returned results against their SHA-256 manifest.

```powershell
wsl -d Ubuntu-22.04 -u root --cd /home/jcondos/comm_nets_e2_20261023 -- bash scripts/run_e2_live.sh --config experiments/configs/e2_live_complete.json --output results/e2_live_new_run
```

The independent validator checks source schedule and realized counts, ROS graph,
fixed parameters, one-worker FIFO order, service hold, timestamps, accounting,
queue integrals and finite-window boundary corrections. After execution inspect
the per-run cleanup records and `ip netns list`; experiment resources must have
been removed. Packet capture loss and application queue drops are separate.

## Focused implementation checks

```powershell
python -B -m unittest discover -s experiments -p test_e2_model.py -v
python -B -m unittest discover -s experiments -p test_e2_live.py -v
```

Report figures use five run-level statistics per condition with minimum/maximum
bars, not confidence intervals or pooled-message percentiles. E2A simulated
time and E2B host timing must remain explicitly separated. Gateway W includes
the worker's response parsing and completion-state update; a separate timestamp
records complete response-body receipt. Neither is pure one-way network delay.

## Retained results and archive verification

The formal E2A dataset remains in `results/e2_model_complete` with its existing
85-file relative SHA-256 manifest. The formal E2B dataset contains 532 manifested
files, including all 20 captures, message logs, configurations, source snapshots,
independent validation and the final network cleanup audit. Full raw E2B data
occupies approximately 1.94 GB and is retained at:

```
C:\Users\james\Documents\ELEN90061_E2\results\e2_live_complete
/home/jcondos/comm_nets_e2_20261023/results/e2_live_complete
```

The workspace has limited free space. It therefore contains a complete compressed
archive rather than a partial `results/e2_live_complete` directory:

- `results/e2_live_complete.tar.gz` (127,470,043 bytes).
- `results/e2_live_complete.tar.gz.sha256` (archive checksum).
- `results/e2_live_complete.sha256_manifest.json` (raw relative file checksums).
- `results/e2_retention.json` (archive hashes, locations and copy verification).

The formal archive SHA-256 is
`b4e0ae7510df05b14dd02b4e036e54dc9087761b918a37679c508489bf937ebc`.
Its manifest SHA-256 is
`eb97db8d8a35877dda3e676c5e619e4da242de19b62ad67cafba3be096090798`.
The extracted Windows copy was checked against every file hash and the exact
manifested file set, and the compressed copies on C: and G: match the archive
hash calculated in Linux.

Both pilots are also retained in complete compressed archives, with separate
checksums and manifests, and as full working copies under the same C: and Linux
results directories. `e2_live_pilot` preserves the failed quiet-tail recording
attempt (83 manifested files); `e2_live_pilot2` contains the corrected four-case
pilot (115 manifested files). Neither contributes to formal statistics. The
source now waits until the absolute observation horizon, including the final
OFF interval, and the gateway independently enforces measurement completion.
See `E2_PILOT_DECISION.md` for the diagnosis and retained pilot results.

To restore a separate copy and verify it:

```powershell
Get-FileHash results/e2_live_complete.tar.gz -Algorithm SHA256
python -m tarfile -e results/e2_live_complete.tar.gz C:/Users/james/Documents/E2_restored
python -B scripts/e1_results_manifest.py C:/Users/james/Documents/E2_restored/results/e2_live_complete --verify
```

The archive contains the `results/e2_live_complete` directory, so the extraction
destination is the parent of `results`. Choose a fresh destination. Run the
manifest verifier after extraction; do not re-run validators against a frozen
retained copy unless intentionally creating an updated provenance version.

To rebuild report assets from the retained formal datasets:

```powershell
python -B scripts/report_e2.py --model results/e2_model_complete --live C:/Users/james/Documents/ELEN90061_E2/results/e2_live_complete --output report/e2
```

All 20 formal live application checks passed. Strict capture checks reconciled
358,031 HTTP exchanges, including warm-up and drain, with zero capture drops.
The source-time measurement cohorts contain 306,896 messages, with zero FIFO
drops, missing ROS messages, request errors or unresolved outcomes. All owned
network namespaces/interfaces and E2 runner/server processes were absent at the
final cleanup audit; the primary default route was unchanged.
