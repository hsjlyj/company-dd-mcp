"""命令行入口：不依赖 MCP 客户端也能跑通全流程（便于脚本化和排障）。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .collect import Engine
from .config import Config
from .ledger import ReportStore
from .manual import ManualFinding, ManualStore, reevaluate_report
from .report import render_html, render_markdown


def _engine() -> Engine:
    return Engine(Config())


def cmd_probe(args: argparse.Namespace) -> int:
    statuses = _engine().registry.probes()
    rows = [s.to_dict() for s in statuses]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"{'源':<34}{'类型':<12}{'辖区':<8}{'状态':<10}{'HTTP':<6}说明")
    for s in rows:
        print(
            f"{s['label'][:32]:<34}{s['kind']:<12}{s['jurisdiction']:<8}{s['status']:<10}"
            f"{str(s.get('http_status') or '-'):<6}{(s.get('detail') or '')[:60]}"
        )
    ok = sum(1 for s in rows if s["status"] == "ok")
    blocked = sum(1 for s in rows if s["status"] == "blocked")
    needs = sum(1 for s in rows if s["status"] == "needs_key")
    print(f"\n可用 {ok}｜被拦截 {blocked}｜需 Key {needs}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    data = _engine().resolve(args.name)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for i, c in enumerate(data["candidates"], 1):
        anchor = c.get("code") or c.get("cik") or c.get("qid") or "-"
        print(f"{i}. [{c.get('kind')}] {c.get('name')}  锚点={anchor}  匹配={'精确' if c.get('exact_name_match') else '模糊'}")
    if not data["candidates"]:
        print("未匹配到候选主体：请改用企业全称或统一社会信用代码。")
    for e in data["errors"]:
        print(f"  ! {e.get('source', e.get('code'))}: {e.get('message')}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .server import _write_files

    eng = _engine()
    rep = eng.build_report(
        args.name,
        announcement_pages=args.pages,
        page_size=args.page_size,
        window_days=args.window_days,
    )
    paths = _write_files(rep)
    payload = rep.to_dict()
    (Config().home / "reports" / f"{rep.report_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.print_md:
        print(render_markdown(payload))
    else:
        print(f"报告：{paths['md']}")
        print(f"结论：{rep.verdict} —— {rep.verdict_reason}")
        print(f"证据：{payload['evidence_count']} 条；未验证清单 {len(payload['gaps'])} 项")
    return 0


def cmd_html(args: argparse.Namespace) -> int:
    from .server import _write_files

    rep = _engine().build_report(args.name, announcement_pages=args.pages)
    paths = _write_files(rep)
    print(paths["html"])
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    cat = _engine().registry.catalog()
    if args.json:
        print(json.dumps(cat, ensure_ascii=False, indent=2))
        return 0
    for s in cat:
        key = f"（需 {s['requires_key']}）" if s.get("requires_key") else ""
        print(f"- [{s['kind']}] {s['label']}{key}\n    {s['homepage']}\n    能力：{', '.join(s['capabilities'])}")
    return 0


def cmd_manual_record(args: argparse.Namespace) -> int:
    """保存一条可复核人工结论，并立即回写报告 verdict/rule。"""
    cfg = Config()
    store = ReportStore(cfg.home)
    data = store.load(args.report_id)
    finding = ManualFinding(
        rule_id=args.rule_id,
        outcome=args.outcome,
        operator=args.operator,
        method=args.method,
        url=args.url,
        attachment=args.attachment,
        note=args.note,
    )
    manual_store = ManualStore(cfg.home)
    path = manual_store.add(args.report_id, finding)
    updated = reevaluate_report(data, manual_store.load(args.report_id))
    report_path = cfg.home / "reports" / f"{args.report_id}.json"
    report_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    # 读回确认更新的是指定报告，而不是只确认写入动作返回成功。
    verified = store.load(args.report_id)
    rule = next(r for r in verified.get("rules", []) if r.get("rule_id") == args.rule_id)
    print(json.dumps({
        "report_id": verified.get("report_id"), "manual_path": str(path),
        "rule_id": args.rule_id, "status": rule.get("status"),
        "verdict": verified.get("verdict"), "verdict_reason": verified.get("verdict_reason"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_manual_show(args: argparse.Namespace) -> int:
    rows = ManualStore(Config().home).load(args.report_id)
    print(json.dumps({"report_id": args.report_id, "findings": rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = ReportStore(Config().home).list(args.limit)
    for r in rows:
        print(f"{r['generated_at']}  {r['verdict']:<13}{r['name']}  {r['report_id']}")
    if not rows:
        print("暂无报告。")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    data = ReportStore(Config().home).load(args.report_id)
    print(render_markdown(data))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import main

    main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="company-dd", description="企业背调 MCP / CLI（证据优先）")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="以 stdio 启动 MCP 服务")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("probe", help="探测所有数据源可用性")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("sources", help="列出数据源目录")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sources)

    sp = sub.add_parser("resolve", help="公司名消歧")
    sp.add_argument("name")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_resolve)

    for name, func, help_text in (
        ("report", cmd_report, "生成完整背调报告（md/html/json）"),
        ("html", cmd_html, "只生成 HTML 报告并打印路径"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("name")
        sp.add_argument("--pages", type=int, default=4, help="公告翻页数（每页最多 30 条）")
        sp.add_argument("--page-size", type=int, default=30)
        sp.add_argument("--window-days", type=int, default=180, help="判定「无该类公告」要求的覆盖天数")
        if name == "report":
            sp.add_argument("--print-md", action="store_true")
        sp.set_defaults(func=func)

    sp = sub.add_parser("manual-record", help="回填一条人工核查结论并重算报告")
    sp.add_argument("report_id")
    sp.add_argument("rule_id", choices=[f"RL-{i:02d}" for i in range(1, 13)])
    sp.add_argument("outcome", choices=("clear", "hit"))
    sp.add_argument("--operator", required=True, help="核查人或系统账号")
    sp.add_argument("--method", required=True, help="核查方法，例如 GSXT 手动检索")
    sp.add_argument("--url", default="", help="官方结果页 URL")
    sp.add_argument("--attachment", default="", help="截图/PDF 的绝对路径")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_manual_record)

    sp = sub.add_parser("manual-show", help="查看报告的人工核查回填")
    sp.add_argument("report_id")
    sp.set_defaults(func=cmd_manual_show)

    sp = sub.add_parser("list", help="列出已生成报告")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="打印报告 Markdown")
    sp.add_argument("report_id")
    sp.set_defaults(func=cmd_show)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
