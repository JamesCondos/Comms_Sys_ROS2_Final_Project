#!/usr/bin/env bash
# Run partner_network TCP trials; no colcon workspace is required.
set -eo pipefail
tcp_trial_dir=$(cd -- "$(dirname -- "$0")" && pwd)
source /opt/ros/humble/setup.bash
set -u
export ROS_DOMAIN_ID=174
export ROS_LOCALHOST_ONLY=1
export PYTHONDONTWRITEBYTECODE=1
exec python3 -B "$tcp_trial_dir/run_trials.py" "$@"
