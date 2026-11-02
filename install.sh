#!/usr/bin/env bash
# 首次安装 / 二次修复：建 venv + 以 editable 方式装本项目（含 dev 依赖），并做一次离线冒烟。
# 设计要点：
#   * 必须真的安装包本身（editable），否则 ./bin/company-dd 与 MCP 客户端在仓库外无法调用；
#   * 冒烟测试默认离线（COMPANY_DD_OFFLINE=1），不发起真实网络请求、不改写 DATA-SOURCES.json。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 $PYTHON；请先安装 Python 3.10+" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "==> 创建虚拟环境 .venv"
  "$PYTHON" -m venv .venv
fi

VENV_PY="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "虚拟环境不可用：$VENV_PY" >&2
  exit 1
fi

if [ -z "${COMPANY_DD_SKIP_UPGRADE:-}" ]; then
  echo "==> 升级 pip（可设 COMPANY_DD_SKIP_UPGRADE=1 跳过）"
  "$VENV_PY" -m pip install --upgrade pip >/dev/null
fi

echo "==> 以 editable 方式安装本项目及开发依赖"
"$VENV_PY" -m pip install -e ".[dev]"

echo "==> 安装校验"
"$VENV_PY" -c "import company_dd; print('company_dd', company_dd.__file__)"

echo "==> 离线冒烟：刷新数据源清单（--offline，不发真实请求）"
COMPANY_DD_OFFLINE=1 "$VENV_PY" tools/refresh_data_sources.py --offline

echo "==> 单测"
"$VENV_PY" -m pytest tests -q

echo
echo "完成。可用命令："
echo "  ./bin/company-dd probe"
echo "  ./bin/company-dd report \"公司全称\" --pages 6"
echo "  ./bin/company-dd sources"
echo "  $VENV_PY tools/mcp_handshake.py   # 协议层交接测试"
