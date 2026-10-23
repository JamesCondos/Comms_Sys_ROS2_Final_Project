#!/usr/bin/env bash
# Run from the project root in Linux; E1 and E2 use separate ROS domains.
set -eo pipefail
source /opt/ros/humble/setup.bash
set -u
export ROS_DOMAIN_ID=163
export ROS_LOCALHOST_ONLY=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/ros2_ws/src/rest_traffic${PYTHONPATH:+:$PYTHONPATH}"
export ROS_LOG_DIR="$PWD/runtime_ros_logs"
mkdir -p "$ROS_LOG_DIR"
exec python3 -B experiments/e2_live.py "$@"
