"""天眼查官方 CLI（tyc）适配器 —— 真实调用，不猜接口。

命令面来自实测（tyc-cli 0.3.8，`tyc <group> <tool> <searchKey>`）：
  company companies <kw>            企业检索（锚定：名称/ID/信用代码）
  company registration-info <k>     核心登记信息（支持信用代码精确查询）
  company change-records <k>        历史变更记录
  risk business-exception <k>       经营异常
  risk judgment-debtor-info <k>     被执行人
  risk dishonest-info <k>           失信被执行人
  risk serious-violation <k>        严重违法失信
  risk administrative-penalty <k>   行政处罚
  risk tax-arrears-notice <k>       欠税公告
  risk high-consumption-restriction <k> 限制高消费
  risk overview <k>                 天眼风险总览

坑（实测踩到）：未登录时 CLI 把错误打到 stdout 且 **exit code 仍是 0**，
所以不能只看返回码，必须解析输出内容并显式判断登录态。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import DdError
from ..models import SourceStatus, now_iso
from .base import Source, SourceSpec

TOOLS: dict[str, tuple[str, str, list[str]]] = {
    # key: (cli 子命令, 中文名, 关联红线)
    "search": ("company companies", "企业检索（锚定）", []),
    "registration": ("company registration-info", "核心登记信息", ["RL-02"]),
    "change_records": ("company change-records", "历史变更记录", ["RL-05"]),
    "business_exception": ("risk business-exception", "经营异常名录", ["RL-02"]),
    "executed": ("risk judgment-debtor-info", "被执行人", ["RL-01"]),
    "dishonest": ("risk dishonest-info", "失信被执行人", ["RL-01"]),
    "serious_violation": ("risk serious-violation", "严重违法失信", ["RL-02"]),
    "admin_penalty": ("risk administrative-penalty", "行政处罚", ["RL-03"]),
    "tax_arrears": ("risk tax-arrears-notice", "欠税公告", ["RL-12", "RL-03"]),
    "high_consumption": ("risk high-consumption-restriction", "限制高消费", ["RL-01"]),
    "risk_overview": ("risk overview", "天眼风险总览", ["RL-01", "RL-02"]),
}

NOT_LOGGED_IN_MARKERS = ("未配置 Authorization", "tyc init", "请先登录", "unauthorized", "401")


class TycCliSource(Source):
    spec = SourceSpec(
        id="tianyancha_cli",
        label="天眼查官方 CLI（tyc）",
        kind="keyed",
        jurisdiction="CN",
        homepage="https://github.com/tyc-tech/tyc-cli",
        capabilities=["cn_registry", "cn_judicial", "cn_change_records"],
        requires_key="TYC_TOKEN",
        note="官方数据源，覆盖工商登记、经营异常、被执行/失信、行政处罚、变更记录；需商务授权。",
    )

    # ------------------------------------------------------------------ 环境
    def binary(self) -> str | None:
        return shutil.which("tyc")

    def config_path(self) -> Path:
        return Path.home() / ".tyc" / "config.json"

    def logged_in(self) -> bool:
        try:
            cfg = json.loads(self.config_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        for key in ("authorization", "Authorization", "token", "accessToken"):
            val = cfg.get(key)
            if isinstance(val, str) and val.strip():
                return True
        headers = cfg.get("headers")
        if isinstance(headers, dict) and headers.get("Authorization"):
            return True
        return False

    def ready(self) -> tuple[bool, str]:
        if not self.binary():
            return False, "本机未安装 tyc CLI：npm i -g tyc-cli"
        if not self.logged_in():
            return False, f"未登录：tyc login（或 tyc init --authorization <KEY>），配置落在 {self.config_path()}"
        return True, "已安装且已登录"

    # ------------------------------------------------------------------ 调用
    def call(self, tool: str, search_key: str, *, page_size: int = 20, timeout: int = 60) -> dict[str, Any]:
        if tool not in TOOLS:
            raise DdError(f"未知工具 {tool}", code="invalid_tool", source=self.id)
        subcmd, label, _ = TOOLS[tool]
        ok, why = self.ready()
        if not ok:
            raise DdError(
                f"{label} 不可调用：{why}",
                code="needs_key",
                source=self.id,
                url=self.spec.homepage,
                hint="安装并登录 tyc CLI 后自动启用；该维度此前只能是未验证。",
            )
        cmd = [self.binary() or "tyc", *subcmd.split(), search_key, "--compact"]
        if tool != "search":
            cmd += ["--pageSize", str(page_size)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise DdError(f"{label} 调用超时（{timeout}s）", code="timeout", source=self.id,
                          retryable=True) from exc
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if any(m in out for m in NOT_LOGGED_IN_MARKERS) or any(m in err for m in NOT_LOGGED_IN_MARKERS):
            raise DdError(
                f"{label} 未授权：{out or err}"[:300],
                code="needs_key", source=self.id, url=self.spec.homepage,
                hint="CLI 未登录；执行 tyc login 后重试。",
            )
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            raise DdError(
                f"{label} 返回的不是 JSON（可能被 --head/--threshold 截断）：{out[:180]}",
                code="parse_error", source=self.id,
            ) from exc
        return {
            "tool": tool,
            "label": label,
            "command": " ".join(cmd[:4]) + " …",
            "data": payload,
            "raw_stdout": out,
        }

    def probe(self) -> SourceStatus:  # type: ignore[override]
        ok, why = self.ready()
        if not ok:
            st = self.needs_key_status()
            st.detail = why
            st.manual_url = self.spec.homepage
            return st
        try:
            res = self.call("registration", os.getenv("TYC_PROBE_KEY", "宁德时代新能源科技股份有限公司"), page_size=1)
            return self.ok(self.spec.homepage, f"已登录且调用成功（{res['label']}）")
        except DdError as exc:
            st = self.needs_key_status()
            st.detail = f"{exc.message} {exc.hint}".strip()
            return st

    # ------------------------------------------------------------------ 采集
    def collect_for_rules(
        self, search_key: str, *, tools: list[str] | None = None, page_size: int = 20
    ) -> dict[str, Any]:
        """按红线需要抓一组工具，返回 {tool: {hits, total, error}}。

        每个工具独立 try：一个工具失败不影响其余，失败原因如实带出。
        """
        wanted = tools or [
            "registration", "business_exception", "executed", "dishonest",
            "serious_violation", "change_records",
        ]
        out: dict[str, Any] = {}
        for t in wanted:
            try:
                res = self.call(t, search_key, page_size=page_size)
                data = res["data"]
                out[t] = {
                    "status": "ok",
                    "label": res["label"],
                    "rules": TOOLS[t][2],
                    "raw": data,
                    "hits": _extract_hits(data),
                }
            except DdError as exc:
                out[t] = {
                    "status": exc.code,
                    "label": TOOLS[t][1],
                    "rules": TOOLS[t][2],
                    "error": exc.message,
                }
        return out


def _extract_hits(data: Any) -> list[dict[str, Any]]:
    """天眼查返回结构不统一，这里只做保守提取：找到第一个像列表的数组。"""
    for key in ("items", "list", "data", "result", "hits"):
        node = data.get(key) if isinstance(data, dict) else None
        if isinstance(node, list):
            return [x for x in node if isinstance(x, dict)][:50]
        if isinstance(node, dict):
            inner = _extract_hits(node)
            if inner:
                return inner
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)][:50]
    return []


def rule_hits(collected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把工具结果折算成「哪条红线拿到了证据」。"""
    out: dict[str, dict[str, Any]] = {}
    for tool, res in collected.items():
        if res.get("status") != "ok":
            continue
        hits = res.get("hits") or []
        for rule_id in res.get("rules") or []:
            slot = out.setdefault(
                rule_id,
                {"source": "tianyancha_cli", "evidence": [], "checked": [], "labels": []},
            )
            slot["checked"].append(res["label"])
            slot["labels"].append(res["label"])
            if hits:
                slot["evidence"].extend(
                    [{"tool": tool, "label": res["label"], "item": h} for h in hits[:5]]
                )
    return out
