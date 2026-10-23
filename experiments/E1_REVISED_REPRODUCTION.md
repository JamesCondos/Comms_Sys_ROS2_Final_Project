# Reproducing the revised E1 experiment

The frozen design is in `E1_REVISED_PROTOCOL.md` and
`configs/e1_revised_complete.json`. It has seven conditions and five repetitions
per condition. The four reference conditions use 1, 2, 4 and 8 actual ROS 2
source nodes. Three additional conditions separately constrain gateway dispatch,
server service time or forward bandwidth at eight sources.

All source nodes share one generator process and one REST gateway. Source count
does not mean HTTP-client count. Each source publishes 2 command, 5 status,
20 odometry and 5 sensor messages per second. Aggregate reference rates are
therefore 32, 64, 128 and 256 messages/s.

## Requirements and execution

The executed environment is Ubuntu 22.04 under WSL2, ROS 2 Humble, Python 3,
`rclpy`, `std_msgs`, `iproute2`, `ping` and `tcpdump`. The network harness needs
root or the corresponding network/capture capabilities. It creates uniquely
named virtual Ethernet interfaces and a network namespace for the server;
it does not shape the workstation's primary network interface.

Run from a Linux copy of the project, using a **new output directory**:

```bash
sudo bash scripts/run_e1_revised.sh \
  --config experiments/configs/e1_revised_complete.json \
  --output results/e1_revised_new_run
```

The wrapper loads ROS, sets an isolated ROS domain and runs the source package
directly from this checkout. It does not need a new colcon build. Network
calibration, ROS graph verification, warm-up, measurement, draining and packet
capture are automatic. Allow approximately 45--50 minutes for 35 runs; the
duration depends on scheduling, setup and drain time. Avoid other intensive
workloads on the shared host during measurement.

On this workstation the retained execution copy is
`/home/jcondos/comm_nets_e1_revised_20261023`. Windows PowerShell can launch it:

```powershell
wsl -d Ubuntu-22.04 -u root --cd /home/jcondos/comm_nets_e1_revised_20261023 -- bash scripts/run_e1_revised.sh --config experiments/configs/e1_revised_complete.json --output results/e1_revised_new_run
```

Google Drive's G: mount is not directly accessible from this WSL environment.
Stage files through a Windows temporary directory under `/mnt/c`, then verify
copied results against the dataset SHA-256 manifest. Do not merge new runs into
the retained formal dataset or replace its raw logs.

## Independent checks

After the runner has stopped, validate application logs and every capture:

```bash
python3 -B experiments/validate_e1_extended.py results/e1_revised_new_run
python3 -B scripts/check_e1_captures.py results/e1_revised_new_run
python3 -B scripts/e1_results_manifest.py results/e1_revised_new_run
python3 -B scripts/e1_results_manifest.py results/e1_revised_new_run --verify
```

Create the manifest only after all validation files have been written. Its
relative paths allow the copied dataset to be verified on Windows. The manifest
command refuses to overwrite an existing manifest. A failed capture check must
be investigated; zero kernel capture drops alone does not prove completeness.

The application validator checks identities, source counts and rates, source
and server clock domains, FIFO order, message conservation, queue capacity,
timestamp ordering, one-request concurrency, metric calculations and source
provenance. Capture validation reconstructs HTTP messages across TCP segments
and reconciles request/response identities with server records, including
warm-up and drain. These capture totals consequently differ from the report's
measurement-cohort totals.

Inspect `ip netns list` after execution and the per-run cleanup records. The
temporary experiment namespaces/interfaces should be absent. A failed setup or
infrastructure check is retained as an invalid attempt, while FIFO overflow is
a valid overload result. Never exclude a run solely for poor performance.

## Figures and report

The report generator requires the complete 35-run matrix and successful
application/capture checks. After validation, run:

```bash
python experiments/report_e1_revised.py results/e1_revised_complete --output report/e1_revised
```

It reads the raw dataset and writes derived tables, standalone figure PDFs,
PNG/SVG previews, a standalone LaTeX document and a reusable LaTeX section into
the report directory. Compile `main.tex` twice and inspect the rendered pages.
The report retains historical `results/e1_complete` figures as a separate
cohort; its three repetitions are never pooled with the five revised repetitions.

Historical figure axes use total mixed messages/s. New source-count axes use
actual ROS 2 nodes. Error bars are observed minimum--maximum ranges of run-level
statistics. They are not confidence intervals. Latency includes successful
measurement-window messages completed during drain, whereas throughput counts
responses completed inside the measurement window. Deadline failures include
lost or unsuccessful generated messages.

## Focused implementation checks

These checks exercise accounting, stage boundaries, real HTTP connection reuse,
injected server service, network ownership and capture reconstruction:

```bash
python3 -B -m unittest discover -s experiments -p test_e1_extended.py -v
python3 -B -m unittest discover -s scripts -p 'test_e1*.py' -v
```

Pilot directories are separate from the final dataset. The first revised pilot
identified incomplete capture tails; the corrected capture lifecycle uses
immediate packet delivery and a quiet drain before stopping tcpdump. The second
pilot verifies the corrected lifecycle. Neither pilot contributes to formal
statistics.
