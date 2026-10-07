#!/bin/bash
# Run from the repository root inside Ubuntu with ROS2 Humble installed.
set -eo pipefail
source /opt/ros/humble/setup.bash
source ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=161
export ROS_LOCALHOST_ONLY=1
experiment_output="results/baseline_$(date -u +%Y%m%dT%H%M%SZ)"
python3 experiments/baseline.py --output "$experiment_output" "$@"
