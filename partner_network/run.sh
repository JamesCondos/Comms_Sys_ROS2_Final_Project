#!/usr/bin/env bash
set -eo pipefail
cd -- "$(dirname -- "$0")"
source /opt/ros/humble/setup.bash
set -u
export ROS_DOMAIN_ID=174
export ROS_LOCALHOST_ONLY=1
export PYTHONDONTWRITEBYTECODE=1
exec python3 -B -m lab.runner "$@"
