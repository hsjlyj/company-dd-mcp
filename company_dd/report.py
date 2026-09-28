"""报告渲染：Markdown / HTML / JSON。三色结论 + 证据台账 + 未验证清单。"""

from __future__ import annotations

import html
import json
from typing import Any

LEVEL_TEXT = {
    "red": "红：命中红线，不建议推进（除有反证）",
    "yellow": "黄：未命中红线但有未验证维度或提示项，需人工补齐后再判断",
    "green": "绿：全部红线维度已核验且未命中",
    "inconclusive": "灰：没有维度被真正核验，无法判断",
}

STATUS_TEXT = {
    "verified": "已核验",
    "partial": "部分核验",
    "unverified": "未查到",
    "blocked": "被拦截，查不了",
    "needs_key": "需配置数据源 Key",
    "not_applicable": "不适用",
    "ok": "可用",
    "error": "报错",
    "hit": "命中",
    "clear": "已核验未命中",
}


def _s(key: str) -> str:
    return STATUS_TEXT.get(key, key)


def render_markdown(rep: dict[str, Any]) -> str:
    subj = rep.get("subject") or {}
    lines: list[str] = []
    lines.append(f"# 企业背调报告：{subj.get('name') or subj.get('query')}")
    lines.append("")
    lines.append(f"- 报告编号：`{rep.get('report_id')}`")
    lines.append(f"- 生成时间：{rep.get('generated_at')}（UTC+8）")
    lines.append(f"- 查询词：{subj.get('query')}")
    lines.append(f"- 主体识别：{subj.get('name')}｜辖区 {subj.get('jurisdiction')}｜置信度 {subj.get('confidence')}")
    if subj.get("ids"):
        lines.append("- 主体锚点：" + "，".join(f"{k}={v}" for k, v in subj["ids"].items() if v))
    lines.append(f"- 消歧说明：{subj.get('disambiguation')}")
    lines.append("")
    lines.append(f"## 结论：{LEVEL_TEXT.get(rep.get('verdict'), rep.get('verdict'))}")
    lines.append("")
    lines.append(f"> {rep.get('verdict_reason')}")
    lines.append("")

    lines.append("## 红线判定")
    lines.append("")
    lines.append("| 规则 | 项目 | 级别 | 状态 | 依据 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in rep.get("rules") or []:
        lines.append(
            f"| {r['rule_id']} | {r['title']} | {'红线' if r['severity'] == 'red_line' else '提示'} "
            f"| {_s(r['status'])} | {r.get('reason', '')} |"
        )
    lines.append("")

    hits = [r for r in (rep.get("rules") or []) if r.get("status") == "hit" and r.get("evidence")]
    if hits:
        lines.append("## 命中项证据")
        lines.append("")
        for r in hits:
            lines.append(f"**{r['rule_id']} {r['title']}**")
            for e in r["evidence"]:
                lines.append(
                    f"- [{e.get('quote') or e.get('pointer')}]({e.get('url')}) "
                    f"｜来源 {e.get('source_label')}｜抓取 {e.get('fetched_at')}"
                    f"｜sha256 {str(e.get('sha256'))[:12]}"
                )
            lines.append("")

    if rep.get("signals"):
        lines.append("## 风险信号")
        lines.append("")
        for s in rep["signals"]:
            tag = {"red": "红", "yellow": "黄", "info": "提示"}.get(s.get("level"), s.get("level"))
            link = f"[原文]({s.get('evidence_url')})" if s.get("evidence_url") else ""
            lines.append(f"- [{tag}] {s.get('title')} ｜{s.get('date')} {link}")
        lines.append("")

    lines.append("## 主体字段")
    lines.append("")
    lines.append("| 字段 | 值 | 状态 | 来源 |")
    lines.append("| --- | --- | --- | --- |")
    for f in (rep.get("fields") or {}).values():
        srcs = "，".join(sorted({e.get("source_label", "") for e in f.get("evidence") or []}))
        val = str(f.get("value"))
        if len(val) > 120:
            val = val[:117] + "..."
        lines.append(f"| {f.get('label')} | {val} | {_s(f.get('status'))} | {srcs or '—'} |")
    lines.append("")

    gaps = rep.get("gaps") or []
    lines.append(f"## 未验证清单（{len(gaps)} 项）")
    lines.append("")
    lines.append("> 未验证 ≠ 安全，也 ≠ 有风险。这些维度本次没拿到证据，结论不能建立在它们之上。")
    lines.append("")
    for f in gaps:
        lines.append(f"- **{f.get('label')}**（{_s(f.get('status'))}）：{f.get('note')}")
    lines.append("")

    lines.append("## 数据源状态（实测）")
    lines.append("")
    lines.append("| 源 | 类型 | 辖区 | 状态 | HTTP | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for s in rep.get("sources") or []:
        lines.append(
            f"| {s.get('label')} | {s.get('kind')} | {s.get('jurisdiction')} | {_s(s.get('status'))} "
            f"| {s.get('http_status') or '—'} | {(s.get('detail') or '')[:80]} |"
        )
    lines.append("")

    if rep.get("manual_steps"):
        lines.append("## 人工必查清单")
        lines.append("")
        for i, step in enumerate(rep["manual_steps"], 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    lines.append("## 边界与免责")
    lines.append("")
    for n in rep.get("notes") or []:
        lines.append(f"- {n}")
    lines.append(f"- 证据条数：{rep.get('evidence_count')}；原始载荷存于 `{rep.get('report_id')}` 对应的 raw 目录，可按 sha256 复核。")
    lines.append("")
    return "\n".join(lines)


def render_html(rep: dict[str, Any]) -> str:
    md = render_markdown(rep)
    color = {"red": "#c62828", "yellow": "#ef6c00", "green": "#2e7d32", "inconclusive": "#546e7a"}.get(
        rep.get("verdict"), "#546e7a"
    )
    rows = []
    for r in rep.get("rules") or []:
        st = r.get("status")
        c = {"hit": "#c62828", "clear": "#2e7d32", "unverified": "#ef6c00"}.get(st, "#666")
        rows.append(
            "<tr>"
            f"<td>{html.escape(r['rule_id'])}</td><td>{html.escape(r['title'])}</td>"
            f"<td>{'红线' if r['severity'] == 'red_line' else '提示'}</td>"
            f"<td style='color:{c};font-weight:600'>{html.escape(_s(st))}</td>"
            f"<td>{html.escape(r.get('reason', ''))}</td></tr>"
        )
    frows = []
    for f in (rep.get("fields") or {}).values():
        frows.append(
            "<tr>"
            f"<td>{html.escape(str(f.get('label')))}</td>"
            f"<td>{html.escape(str(f.get('value'))[:300])}</td>"
            f"<td>{html.escape(_s(f.get('status')))}</td>"
            f"<td>{html.escape('，'.join(sorted({e.get('source_label','') for e in f.get('evidence') or []})))}</td>"
            "</tr>"
        )
    srows = []
    for s in rep.get("sources") or []:
        srows.append(
            "<tr>"
            f"<td>{html.escape(str(s.get('label')))}</td><td>{html.escape(str(s.get('kind')))}</td>"
            f"<td>{html.escape(_s(s.get('status')))}</td><td>{html.escape(str(s.get('http_status') or '—'))}</td>"
            f"<td>{html.escape((s.get('detail') or '')[:200])}</td></tr>"
        )
    gaps = "".join(
        f"<li><b>{html.escape(str(f.get('label')))}</b>（{html.escape(_s(f.get('status')))}）："
        f"{html.escape(str(f.get('note')))}</li>"
        for f in rep.get("gaps") or []
    )
    steps = "".join(f"<li>{html.escape(str(x))}</li>" for x in rep.get("manual_steps") or [])
    sig = "".join(
        f"<li>[{'红' if s.get('level') == 'red' else '黄' if s.get('level') == 'yellow' else '提示'}] "
        f"{html.escape(str(s.get('title')))} "
        f"<a href='{html.escape(str(s.get('evidence_url') or '#'))}'>原文</a></li>"
        for s in rep.get("signals") or []
    )
    subj = rep.get("subject") or {}
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>背调报告 {html.escape(str(subj.get('name') or subj.get('query')))}</title>
<style>
 body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f6f7f9;color:#1f2328}}
 .wrap{{max-width:1000px;margin:0 auto;padding:28px 20px 60px}}
 .card{{background:#fff;border-radius:14px;padding:20px 22px;margin:16px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 h1{{font-size:22px;margin:0 0 6px}} h2{{font-size:16px;margin:0 0 10px}}
 .verdict{{border-left:6px solid {color};padding-left:14px}}
 .verdict b{{color:{color};font-size:18px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{padding:7px 8px;border-bottom:1px solid #eceff1;text-align:left;vertical-align:top}}
 th{{background:#fafbfc;font-weight:600}} .muted{{color:#6b7280;font-size:12px}}
 ul{{padding-left:20px}} code{{background:#f2f3f5;padding:1px 5px;border-radius:4px}}
</style></head><body><div class="wrap">
 <div class="card verdict">
   <h1>{html.escape(str(subj.get('name') or subj.get('query')))}</h1>
   <div class="muted">报告 {html.escape(str(rep.get('report_id')))} · {html.escape(str(rep.get('generated_at')))} · 辖区 {html.escape(str(subj.get('jurisdiction')))} · 置信度 {html.escape(str(subj.get('confidence')))}</div>
   <p><b>{html.escape(LEVEL_TEXT.get(rep.get('verdict'), str(rep.get('verdict'))))}</b></p>
   <p>{html.escape(str(rep.get('verdict_reason')))}</p>
 </div>
 <div class="card"><h2>红线判定</h2><table><tr><th>规则</th><th>项目</th><th>级别</th><th>状态</th><th>依据</th></tr>{''.join(rows)}</table></div>
 <div class="card"><h2>风险信号</h2><ul>{sig or '<li>无</li>'}</ul></div>
 <div class="card"><h2>主体字段</h2><table><tr><th>字段</th><th>值</th><th>状态</th><th>来源</th></tr>{''.join(frows)}</table></div>
 <div class="card"><h2>未验证清单</h2><div class="muted">未验证 ≠ 安全，也 ≠ 有风险；这些维度本次没拿到证据。</div><ul>{gaps or '<li>无</li>'}</ul></div>
 <div class="card"><h2>数据源状态（实测）</h2><table><tr><th>源</th><th>类型</th><th>状态</th><th>HTTP</th><th>说明</th></tr>{''.join(srows)}</table></div>
 <div class="card"><h2>人工必查清单</h2><ol>{steps or '<li>无</li>'}</ol></div>
 <div class="card"><h2>边界</h2><ul>{''.join(f'<li>{html.escape(str(n))}</li>' for n in rep.get('notes') or [])}</ul>
 <div class="muted">证据条数 {rep.get('evidence_count')}；原始载荷按 sha256 可复核。</div></div>
</div></body></html>"""


def render_json(rep: dict[str, Any]) -> str:
    return json.dumps(rep, ensure_ascii=False, indent=2)
