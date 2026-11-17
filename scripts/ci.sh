#!/usr/bin/env bash
# P2-12 契约 CI：不访问网络、不改数据库；发布门禁在 publish 内执行。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/3] 语法编译"
python3 -m compileall -q fundana

echo "[2/3] 单元测试"
python3 -m unittest discover -s tests

echo "[3/3] 前后端契约"
python3 scripts/check_ui_contract.py

echo "CI OK"
