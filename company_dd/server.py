"""MCP 服务端（mcp 2.x：mcp.server.mcpserver.MCPServer）。

工具面刻意做小：每个工具的输出都能被人复核。凡是查不到的维度，
输出里必须出现 blocked / needs_key / unverified 与原因，不允许静默省略。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from . import __version__
from .collect import Engine
from .config import Config
from .ledger import ReportStore
from .manual import ManualFinding, ManualStore, reevaluate_report
from .models import Report
from .report import render_html, render_markdown
from .sources.official_cn import OfficialCnSource

config = Config()
engine = Engine(config)
store = ReportStore(config.home)

server = MCPServer(
    name="company-dd",
    title="企业背调 MCP",
    version=__version__,
    instructions=(
        "证据优先的企业背调工具。使用顺序：先 probe_sources 看哪些渠道可用，"
        "再 resolve_company 消歧，然后 build_report 出结论。"
        "未验证维度必须在回答中原样呈现，不得当作通过。"
    ),
)


async def _run(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))


@server.tool()
async def probe_sources() -> dict[str, Any]:
    """探测全部数据源的实时可用性（真发请求）。

    返回每个源的状态：ok / blocked / needs_key / error，含真实 HTTP 状态码。
    先跑这个，才知道这一次能查什么、什么必须转人工。
    """
    statuses = await _run(engine.registry.probes)
    return {
        "checked_at": statuses[0].fetched_at if statuses else "",
        "sources": [s.to_dict() for s in statuses],
        "catalog": engine.registry.catalog(),
        "summary": {
            "ok": sum(1 for s in statuses if s.status == "ok"),
            "blocked": sum(1 for s in statuses if s.status == "blocked"),
            "needs_key": sum(1 for s in statuses if s.status == "needs_key"),
        },
    }


@server.tool()
async def resolve_company(name: str) -> dict[str, Any]:
    """把公司名消歧成确定的调查主体（上市公司代码 / CIK / QID）。

    返回候选列表和各自的锚点。同名主体必须先用统一社会信用代码核对，
    再开始采集，否则红线判定会张冠李戴。
    """
    return await _run(engine.resolve, name)


@server.tool()
async def build_report(
    name: str,
    announcement_pages: int = 4,
    page_size: int = 30,
    window_days: int = 180,
    save_files: bool = True,
) -> dict[str, Any]:
    """对一家公司做完整背调：探测→消歧→采集→红线判定→出结论。

    announcement_pages × page_size 决定公告扫描范围；window_days 是判定
    「近 N 天无某类公告」所要求的覆盖窗口，窗口没覆盖到就会被标成未验证。
    """
    rep: Report = await _run(
        engine.build_report, name,
        announcement_pages=max(1, min(announcement_pages, 20)),
        page_size=max(10, min(page_size, 30)),
        window_days=max(30, min(window_days, 1825)),
    )
    payload = rep.to_dict()
    out: dict[str, Any] = {"report": payload}
    if save_files:
        out["files"] = await _run(_write_files, rep)
        out["report_path"] = str(await _run(store.save, rep))
    return out


@server.tool()
async def risk_sweep(name: str, pages: int = 3) -> dict[str, Any]:
    """只做风险信号扫描：拉最近公告标题，按红线关键词命中列出。

    适合快速初筛。返回每条的日期、标题、原文链接和命中的规则编号。
    """
    rep: Report = await _run(
        engine.build_report, name, announcement_pages=max(1, min(pages, 10)), page_size=50
    )
    return {
        "subject": rep.subject.to_dict(),
        "verdict": rep.verdict,
        "verdict_reason": rep.verdict_reason,
        "scanned_announcements": (rep.fields.get("announcement_sweep").value
                                  if rep.fields.get("announcement_sweep") else None),
        "signals": [s.to_dict() for s in rep.signals],
        "rules": [r.to_dict() for r in rep.rules],
        "gaps": [f.to_dict() for f in rep.gaps()],
    }


@server.tool()
async def manual_checklist(name: str) -> dict[str, Any]:
    """生成人工必查清单：官方渠道入口 + 检索词 + 留痕要求。

    被 WAF/验证码拦住的维度必须由人过一遍，工具不代替人在官方系统里核对。
    """
    return {
        "subject": name,
        "steps": OfficialCnSource.manual_plan(name),
        "note": "人工核对结果请留截图/PDF 与核查时间，作为报告附件。",
    }


@server.tool()
async def manual_record(
    report_id: str,
    rule_id: str,
    outcome: str,
    operator: str,
    method: str,
    url: str = "",
    attachment: str = "",
    note: str = "",
) -> dict[str, Any]:
    """将人工核查结果留痕并回写报告。

    outcome 只能是 clear 或 hit；必须同时提供核查人、方法，以及官方 URL 或截图/PDF 路径。
    这是唯一允许人工结果改变规则状态的入口，不能直接编辑报告 JSON。
    """
    if not rule_id.startswith("RL-") or outcome not in ("clear", "hit"):
        return {"error": "rule_id 必须形如 RL-01，outcome 只能是 clear/hit", "code": "invalid_input"}
    try:
        def _record() -> dict[str, Any]:
            data = store.load(report_id)
            finding = ManualFinding(
                rule_id=rule_id, outcome=outcome, operator=operator,
                method=method, url=url, attachment=attachment, note=note,
            )
            manual_store = ManualStore(config.home)
            manual_store.add(report_id, finding)
            updated = reevaluate_report(data, manual_store.load(report_id))
            path = config.home / "reports" / f"{report_id}.json"
            path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            verified = store.load(report_id)
            rule = next((r for r in verified.get("rules", []) if r.get("rule_id") == rule_id), None)
            if rule is None:
                return {"error": f"报告中不存在规则 {rule_id}", "code": "rule_not_found"}
            return {
                "report_id": verified.get("report_id"), "manual_path": str(manual_store.path(report_id)),
                "rule": rule, "verdict": verified.get("verdict"),
                "verdict_reason": verified.get("verdict_reason"),
            }
        return await _run(_record)
    except FileNotFoundError as exc:
        return {"error": str(exc), "code": "not_found"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "code": getattr(exc, "code", "invalid_input")}


@server.tool()
async def manual_findings(report_id: str) -> dict[str, Any]:
    """读取某份报告已回填的人工核查证据。"""
    try:
        return {"report_id": report_id, "findings": await _run(ManualStore(config.home).load, report_id)}
    except ValueError as exc:
        return {"error": str(exc), "code": "invalid_input"}


@server.tool()
async def list_reports(limit: int = 20) -> dict[str, Any]:
    """列出本机已生成的背调报告。"""
    return {"reports": await _run(store.list, max(1, min(limit, 100)))}


@server.tool()
async def get_report(report_id: str) -> dict[str, Any]:
    """按报告编号读取完整报告 JSON（含证据台账索引）。"""
    try:
        return {"report": await _run(store.load, report_id)}
    except FileNotFoundError as exc:
        return {"error": str(exc), "code": "not_found"}
    except ValueError as exc:
        return {"error": str(exc), "code": "invalid_input"}


@server.tool()
async def get_evidence(report_id: str) -> dict[str, Any]:
    """读取报告的证据台账：来源、URL、抓取时间、sha256、原始载荷路径。

    复核时先看这里：拿 URL 回原站核对，或比对 raw 目录里的原始载荷哈希。
    """
    try:
        data = await _run(store.load, report_id)
    except FileNotFoundError as exc:
        return {"error": str(exc), "code": "not_found"}
    except ValueError as exc:
        return {"error": str(exc), "code": "invalid_input"}
    items: list[dict[str, Any]] = []
    for f in (data.get("fields") or {}).values():
        items.extend(f.get("evidence") or [])
    for r in data.get("rules") or []:
        items.extend(r.get("evidence") or [])
    seen = set()
    unique = []
    for e in items:
        key = (e.get("url"), e.get("sha256"), e.get("pointer"), e.get("quote"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return {"report_id": data.get("report_id"), "count": len(unique), "evidence": unique}


def _write_files(rep: Report) -> dict[str, str]:
    payload = rep.to_dict()
    base = config.home / "reports" / rep.report_id
    paths = {
        "md": base.with_suffix(".md"),
        "html": base.with_suffix(".html"),
        "json": base.with_suffix(".json"),
    }
    paths["md"].write_text(render_markdown(payload), encoding="utf-8")
    paths["html"].write_text(render_html(payload), encoding="utf-8")
    paths["json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
