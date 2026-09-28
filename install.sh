#!/usr/bin/env bash
# company-dd 首次安装：本机没有 pip/venv 也能装（用 get-pip.py 给 venv 装 pip）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
cd "$ROOT"

echo "== 解释器: $($PY -V 2>&1)"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "== 创建 venv（--without-pip，再用 get-pip 注入 pip）"
  "$PY" -m venv --without-pip "$ROOT/.venv"
  curl -sS -o "$ROOT/.venv/get-pip.py" https://bootstrap.pypa.io/get-pip.py
  "$ROOT/.venv/bin/python" "$ROOT/.venv/get-pip.py" -q
fi

echo "== 安装依赖"
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q mcp httpx anyio pytest

echo "== 冒烟测试"
"$ROOT/.venv/bin/python" -m pytest tests -q
"$ROOT/.venv/bin/python" tools/refresh_data_sources.py

cat <<EOF

安装完成。下一步：
  ./bin/company-dd probe
  ./bin/company-dd report "宁德时代" --pages 6
  MCP 接入见 README.md 第 2 节
EOF
