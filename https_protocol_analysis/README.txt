SHARED ROS 2 / HTTPS NETWORK

This folder contains the complete shared runtime. It does not import code from
partner_network, complex_experiments or complex_network_ws. A colcon build is
not needed. See the root README for the ROS and Python dependency setup.

settings.py       Traffic rates, queue sizes, timing and network profiles.
components.py     ROS sources/gateways, site edges, a proxy and two workers.
transport.py      HTTPX/Hypercorn for HTTP/1.1 and HTTP/2; aioquic for HTTP/3.
video_frames.py   Loads the project MP4 as JPEG frames before timing starts.
network.py        Isolated IP routing, WAN shaping, packet captures and cleanup.
runner.py         Starts the processes, sets their common clock window and stops them.
metrics.py        Reads message logs and calculates the result summaries.
test_runtime.py   Small checks for transports, failure handling and accounting.

The application path is:
Robots 1–2 -> Edge A -> Router A -> central proxy -> Workers 1 and 2
Robots 3–4 -> Edge B -> Router B -> central proxy -> Workers 1 and 2

Both site uplinks support delay, loss and forward bandwidth limits. All hosts
are Linux namespaces on one computer. The edges forward individual messages;
they do not filter or combine payloads. The proxy has one FIFO and one service
loop targeting each worker. Each worker processes one message at a time.

RUNNING A SHARED EXPERIMENT

From the repository root, source ROS and activate the configured interpreter:

source /opt/ros/humble/setup.bash
sudo -E https_protocol_analysis/.venv/bin/python -m https_protocol_analysis.runner run --protocol h2 --profile clean --seconds 5 --output https_protocol_analysis/results/h2_smoke_01

Profiles: clean, delay, loss, rate, combined. --load is the total messages/s
across the four robots. Their count stays fixed. Default mixed traffic is
command/status/odometry/sensor/video at 2/5/20/5/5 per robot, or 148 messages/s.
The camera frames come only from:
dependencies/FPV Drone Flight through Beautiful Iceland Canyon - Joshua Turner (1080p).mp4
The whole clip is sampled at 5 frames/s, resized to 320 x 180 and JPEG encoded
at quality 80 before the run starts. Frames repeat when the clip ends. Increasing
traffic load also speeds up frame publication. OpenCV (python3-opencv) decodes
the clip; no synthetic-image fallback or alternative video source is provided.
The TCP runner also supports --sources 1: only robot1 and edgeA are active,
with one edge-to-proxy TCP connection and a base rate of 37 messages/s.
Changing load multiplies all rates equally. Random mode is available through
settings.make_config and uses independent Poisson probes instead of mixed data.

The runner's --config accepts a complete JSON configuration generated from
settings.make_config(). All runs require a fresh output directory. Do not run
performance experiments concurrently. --loopback is a local application check:
it creates no routers, applies no impairments and disables packet capture.
Loopback data must not be presented as routed WAN measurements.

MEASUREMENTS

Source IDs, source/scheduled timestamps, queue admission/dequeue, request and
response times are saved as small JSON-lines records under logs/. Payloads are
omitted from logs. Workers ACK only after processing, and message IDs survive
all hops. Success counts and latency use source-time measurement cohorts,
including successes completed during the bounded drain. Throughput and goodput
count responses completed inside measurement. Queue length is time weighted.
Per-class deadline denominators include missing, failed and dropped messages.

summary.json and validation.json contain measurements and accounting checks.
timeseries.csv contains one-second application throughput, including drain.
network/ contains qdisc snapshots, both WAN captures and cleanup records.
source_snapshot/ and source_sha256.json retain the Python implementation used.
The certificate is generated per run and verified by clients; private_key.pem
is local-only and ignored by Git.

TCP socket logs record the requested and actual algorithm, RTT, congestion
window, cumulative transmitted bytes and cumulative retransmissions. Samples are taken every 0.2 seconds
once sockets are observed. A socket that closes before sampling can be missed;
these are observed kernel counters, not a full packet-retransmission analysis.
Capture recorder drops are separate from network packet loss/retransmissions.
When ethtool is absent, offloads are not disabled; do not treat virtual capture
records as physical wire frames. Telemetry is synthetic; camera payloads are frames from the supplied MP4.

RECOVERY

Normal completion and Ctrl+C stop processes and remove owned resources. If a
run is interrupted, retain its logs and inspect invalid.json before reusing it.
The cleanup command checks process identities and resource ownership:

sudo -E https_protocol_analysis/.venv/bin/python -m https_protocol_analysis.runner cleanup --output PATH_TO_ONE_RUN

To remove dependencies on these files, update the TCP runner too. Both sets of
experiments must use this single runtime to make controlled comparisons.
