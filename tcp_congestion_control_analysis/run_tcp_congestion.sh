#!/usr/bin/env bash
# Run the shared HTTPS network with the TCP trial settings below.
set -eo pipefail
tcp_trial_dir=$(cd -- "$(dirname -- "$0")" && pwd)
source /opt/ros/humble/setup.bash
set -u
export ROS_DOMAIN_ID=174
export ROS_LOCALHOST_ONLY=1
export PYTHONDONTWRITEBYTECODE=1
# Prefer the shared virtual environment, including when this script is run with sudo.
tcp_python=${TCP_PYTHON:-"$tcp_trial_dir/../https_protocol_analysis/.venv/bin/python"}
if [ ! -x "$tcp_python" ]; then
    tcp_python=python3
fi
exec "$tcp_python" -B "$tcp_trial_dir/run_trials.py" "$@"
