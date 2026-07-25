#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
CONFIG_PATH="${XHS_CONFIG:-config/config.json}"

if [ ! -f "$CONFIG_PATH" ]; then
  cp config/config.example.json config/config.json
  CONFIG_PATH="config/config.json"
  echo "已创建本地配置: config/config.json"
  echo "请先填写 sample_note_url 或 profile_url，再重新运行。"
fi

python3 scripts/xhs_backup.py --config "$CONFIG_PATH" "$@"
