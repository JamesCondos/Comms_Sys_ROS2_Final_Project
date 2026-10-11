#!/bin/bash
# Run the same ROS2-to-REST workload with Reno, CUBIC, and BBR.
# This selects TCP per HTTP socket; it does not change the WSL-wide default.
set -eo pipefail

source /opt/ros/humble/setup.bash
source ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=161
export ROS_LOCALHOST_ONLY=1

experiment_output="results/tcp_congestion_$(date -u +%Y%m%dT%H%M%SZ)"
python3 experiments/baseline.py \
    --output "$experiment_output" \
    --tcp reno cubic bbr \
    "$@"
