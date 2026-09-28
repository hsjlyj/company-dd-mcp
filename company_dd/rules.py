"""红线规则引擎。

三种判定，语义严格区分：
  hit        —— 有可复核证据命中
  clear      —— 覆盖该维度的源确实查过且 0 命中（必须在 reason 里写清查了多少条）
  unverified —— 源被拦截 / 缺 Key / 没有覆盖，查不了

结论颜色只看 red_line：
  任一 red_line=hit → red
  无 hit 但有 red_line=unverified → 最多 yellow（未验证不等于通过）
  全部 red_line=clear 且至少一条被真正核验过 → green
  什么都没采集到 → inconclusive
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import RULE_CLEAR, RULE_HIT, RULE_UNVERIFIED, Evidence, Report, RuleResult

RULES_PATH = Path(__file__).with_name("rules.json")


def load_rules(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or RULES_PATH).read_text(encoding="utf-8"))


@dataclass
class SweepScope:
    """一次公告扫描的实际范围，用于 clear 判定的措辞与可复核性。

    clear 必须同时满足：源可用 + 命中数低于阈值 + 扫描窗口真的覆盖到了截止日。
    只扫到最近的几十条就宣称「近一年无风险公告」是耍流氓，这里是硬约束。
    """

    source_id: str = ""
    source_label: str = ""
    scanned: int = 0
    url: str = ""
    ok: bool = False
    detail: str = ""
    date_from: str = ""      # 最早一条公告日期
    date_to: str = ""        # 最新一条公告日期
    window_days: int = 0     # 本次要求覆盖的天数
    covers_window: bool = False

    def scope_text(self) -> str:
        if self.date_from and self.date_to:
            return f"{self.scanned} 条公告（{self.date_from} ~ {self.date_to}）"
        return f"{self.scanned} 条公告"


def _match(patterns: list[str], titles: list[str]) -> list[int]:
    idx: list[int] = []
    if not patterns:
        return idx
    regex = re.compile("|".join(re.escape(p) for p in patterns), re.IGNORECASE)
    for i, t in enumerate(titles):
        if regex.search(t or ""):
            idx.append(i)
    return idx


def _announcement_evidence(hits: list[dict[str, Any]], source_label: str) -> list[Evidence]:
    from .models import now_iso

    out = []
    for h in hits:
        out.append(
            Evidence(
                source_id="cninfo",
                source_label=source_label,
                url=h.get("url") or "https://www.cninfo.com.cn/",
                fetched_at=now_iso(),
                pointer=f"公告标题包含「{h.get('matched')}」",
                quote=f"{h.get('date')} {h.get('title')}",
            )
        )
    return out


def evaluate(
    report: Report,
    *,
    announcements: list[dict[str, Any]] | None = None,
    sweep: SweepScope | None = None,
    financials: dict[str, Any] | None = None,
    filings: list[dict[str, Any]] | None = None,
    keyed_hits: dict[str, Any] | None = None,
    rules_doc: dict[str, Any] | None = None,
    jurisdiction: str = "unknown",
) -> tuple[list[RuleResult], str, str]:
    rules_doc = rules_doc or load_rules()
    announcements = announcements or []
    filings = filings or []
    keyed_hits = keyed_hits or {}
    titles = [a.get("title", "") for a in announcements]
    results: list[RuleResult] = []

    for rule in rules_doc["rules"]:
        rid = rule["id"]
        scope = rule.get("scope", "")
        patterns = rule.get("patterns") or []
        threshold = int(rule.get("threshold") or 1)

        # 1) Key 源/官方 CLI 直接给出了该维度的结构化结果
        kh = keyed_hits.get(rid) or {}
        if kh:
            evidence = []
            for item in (kh.get("evidence") or [])[:5]:
                payload = json.dumps(item.get("item"), ensure_ascii=False)[:400]
                evidence.append(
                    Evidence(
                        source_id=kh.get("source", "keyed"),
                        source_label=kh.get("source_label") or kh.get("source", "商业数据源"),
                        url=kh.get("url", ""),
                        fetched_at=report.generated_at,
                        pointer=f"{item.get('tool')} / {item.get('label')}",
                        quote=payload,
                    )
                )
            if evidence:
                results.append(
                    RuleResult(
                        rule_id=rid, title=rule["title"], severity=rule["severity"],
                        status=RULE_HIT,
                        reason=f"数据源 {kh.get('source_label', kh.get('source'))} 在"
                               f"{'、'.join(kh.get('labels') or kh.get('checked') or ['相关维度'])}中返回记录，"
                               "需按主体名称/信用代码核对是否为同一主体。",
                        evidence=evidence,
                        manual_url=rule.get("manual_url", ""),
                    )
                )
            else:
                results.append(
                    RuleResult(
                        rule_id=rid, title=rule["title"], severity=rule["severity"],
                        status=RULE_CLEAR,
                        reason=f"已通过 {kh.get('source_label', kh.get('source'))} 核查"
                               f"{'、'.join(kh.get('checked') or [])}，无记录。",
                        manual_url=rule.get("manual_url", ""),
                    )
                )
            continue

        # 1b) 兼容旧形态：exec_or_breach 这种聚合键
        if rid == "RL-01" and keyed_hits.get("exec_or_breach"):
            payload = keyed_hits["exec_or_breach"]
            results.append(
                RuleResult(
                    rule_id=rid, title=rule["title"], severity=rule["severity"],
                    status=RULE_HIT,
                    reason=f"{payload.get('source_label')} 返回了执行/失信记录；需按案号核对主体一致性。",
                    evidence=[], manual_url=rule.get("manual_url", ""),
                )
            )
            continue

        # 2) 公告类规则（RL-08 在美国主体下改走 SEC 申报分支）
        if scope == "announcements" and not (
            rid == "RL-08" and filings and not (sweep and sweep.ok)
        ):
            if sweep and sweep.ok:
                idx = _match(patterns, titles)
                if len(idx) >= threshold:
                    detailed = []
                    for i in idx:
                        item = dict(announcements[i])
                        item["matched"] = next(
                            (p for p in patterns if p.lower() in (titles[i] or "").lower()), patterns[0]
                        )
                        detailed.append(item)
                    results.append(
                        RuleResult(
                            rule_id=rid, title=rule["title"], severity=rule["severity"],
                            status=RULE_HIT,
                            reason=f"在扫描范围 {sweep.scope_text()} 内命中 {len(idx)} 条（阈值 {threshold}）。",
                            evidence=_announcement_evidence(detailed, sweep.source_label),
                            manual_url=rule.get("manual_url", ""),
                        )
                    )
                elif sweep.covers_window:
                    results.append(
                        RuleResult(
                            rule_id=rid, title=rule["title"], severity=rule["severity"],
                            status=RULE_CLEAR,
                            reason=f"扫描范围 {sweep.scope_text()}（{sweep.source_label}，{sweep.url}），"
                                   f"命中 {len(idx)} 条，低于阈值 {threshold}，且窗口已覆盖 {sweep.window_days} 天。",
                            manual_url=rule.get("manual_url", ""),
                        )
                    )
                else:
                    results.append(
                        RuleResult(
                            rule_id=rid, title=rule["title"], severity=rule["severity"],
                            status=RULE_UNVERIFIED,
                            reason=f"只覆盖到 {sweep.scope_text()}，未达 {sweep.window_days} 天窗口"
                                   f"（命中 {len(idx)} 条），不足以判定该维度。",
                            manual_url=rule.get("manual_url", ""),
                        )
                    )
            else:
                reason = (
                    "该规则绑定中国上市公司公告渠道；本主体为美股，需以 SEC 申报口径"
                    "（见 RL-08）与人工渠道核验，本次未覆盖。"
                    if jurisdiction == "US"
                    else "未能取得公告清单（来源不可用或主体非上市），该维度查不了。"
                )
                results.append(
                    RuleResult(
                        rule_id=rid, title=rule["title"], severity=rule["severity"],
                        status=RULE_UNVERIFIED,
                        reason=reason,
                        manual_url=rule.get("manual_url", ""),
                    )
                )
            continue

        # 2) 申报类规则（美股）：优先于公告类分支，避免被 CN 公告口径吞掉
        if rid == "RL-08" and filings and not (sweep and sweep.ok):
            forms = {f.get("form") for f in filings}
            risky = sorted(forms & {"NT 10-K", "NT 10-Q", "15-12B", "25-NSE", "8-A12B"})
            if risky:
                results.append(
                    RuleResult(
                        rule_id=rid, title=rule["title"], severity=rule["severity"],
                        status=RULE_HIT,
                        reason=f"SEC 申报清单出现风险表单：{', '.join(risky)}。",
                        evidence=[
                            Evidence(
                                source_id="sec_edgar", source_label="SEC EDGAR",
                                url=f.get("url", ""), fetched_at=report.generated_at,
                                pointer=f"form={f.get('form')}",
                                quote=f"{f.get('filing_date')} {f.get('form')}",
                            )
                            for f in filings
                            if f.get("form") in risky
                        ][:5],
                        manual_url=rule.get("manual_url", ""),
                    )
                )
            else:
                results.append(
                    RuleResult(
                        rule_id=rid, title=rule["title"], severity=rule["severity"],
                        status=RULE_CLEAR,
                        reason=f"已核验 {len(filings)} 条 SEC 申报记录，无退市/迟报类表单。",
                        manual_url=rule.get("manual_url", ""),
                    )
                )
            continue

        # 4) 财务类规则
        if scope == "financials":
            if financials:
                hits = [k for k, v in financials.items() if v.get("status") == "ok"]
                results.append(
                    RuleResult(
                        rule_id=rid, title=rule["title"], severity=rule["severity"],
                        status=RULE_CLEAR if hits else RULE_UNVERIFIED,
                        reason=(
                            f"已取到 {len(hits)} 项财务口径（{', '.join(hits)}），需人工按行业基准判断，"
                            "工具不替代估值与偿债能力判断。"
                            if hits
                            else "未取到财务口径。"
                        ),
                        manual_url=rule.get("manual_url", ""),
                    )
                )
            else:
                results.append(
                    RuleResult(
                        rule_id=rid, title=rule["title"], severity=rule["severity"],
                        status=RULE_UNVERIFIED, reason="未取得财务数据（来源不可用或主体未披露）。",
                        manual_url=rule.get("manual_url", ""),
                    )
                )
            continue

        # 5) 官方渠道类（工商 / 执行 / 信用）——默认查不了，如实标记并给人工与 Key 两条路
        manual = rule.get("manual_url", "")
        results.append(
            RuleResult(
                rule_id=rid, title=rule["title"], severity=rule["severity"],
                status=RULE_UNVERIFIED,
                reason=(
                    "覆盖该维度的官方渠道对自动化流量拦截（实测 GSXT 521 / 信用中国 412 / "
                    "执行公开网 403 / 裁判文书网验证码，详见 DATA-SOURCES.json 的 "
                    "blocked_alternatives），且未配置商业数据源。两条出路：接 tyc 或启信宝 Key，"
                    "或用 record 命令把人工核查结果回填（需留痕）。"
                ),
                manual_url=manual,
            )
        )

    return results, *verdict_of(results, jurisdiction=jurisdiction, listing=report.subject.listing)


def verdict_of(
    results: list[RuleResult], *, jurisdiction: str = "unknown", listing: str = ""
) -> tuple[str, str]:
    """结论颜色：只看 red_line。任一红线 unverified → 最高黄。"""
    red_hit = [r for r in results if r.severity == "red_line" and r.status == RULE_HIT]
    red_unver = [r for r in results if r.severity == "red_line" and r.status == RULE_UNVERIFIED]
    red_clear = [r for r in results if r.severity == "red_line" and r.status == RULE_CLEAR]
    warn_hit = [r for r in results if r.severity == "warning" and r.status == RULE_HIT]

    if not results or (not red_clear and not red_hit):
        verdict = "inconclusive"
        if jurisdiction == "GLOBAL" or not listing:
            reason = (
                "该主体未匹配到任何可核验的官方披露渠道（非上市或境外），"
                "本次没有维度能判定：请按人工清单在官方系统核查，或配置商业数据源 Key 后重跑。"
            )
        else:
            reason = "没有任何红线维度被真正核验过，无法给出结论。"
    elif red_hit:
        verdict = "red"
        reason = f"命中 {len(red_hit)} 条红线：" + "、".join(f"{r.rule_id} {r.title}" for r in red_hit)
    elif red_unver:
        verdict = "yellow"
        reason = (
            f"未发现命中，但有 {len(red_unver)} 条红线维度查不了（"
            + "、".join(r.rule_id for r in red_unver)
            + "），结论不能升级为绿。请按人工清单补齐后再判定。"
        )
    elif warn_hit:
        verdict = "yellow"
        reason = f"红线已核验通过，但提示项命中 {len(warn_hit)} 条：" + "、".join(
            f"{r.rule_id} {r.title}" for r in warn_hit
        )
    else:
        verdict = "green"
        reason = f"全部 {len(red_clear)} 条红线维度已核验且未命中（范围见各条 reason）。"
    return verdict, reason
