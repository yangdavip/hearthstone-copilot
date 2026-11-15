#!/bin/bash
# 项目内统一执行入口：以脚本所在目录为根，clone 到任何路径都能跑。
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
exec .venv/bin/python "$@"
