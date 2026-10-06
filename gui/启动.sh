#!/bin/sh
# 课堂笔记智能体 · macOS / Linux 启动脚本
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先安装 Python 3.11+"
  exit 1
fi
python3 gui.py
