"""采集编排：把多个源的事实汇成一份带证据的报告。

原则：
  * 先探测可用性，再决定采什么；探测结果原样进报告。
  * 主体先消歧（统一社会信用代码 / CIK / QID 作为锚点），再采集。
  * 每个写入报告的值都要伴随 ledger 证据；没有证据的字段只能是 unverified/blocked/needs_key。
"""

from __future__ import annotations

import json
from typing import Any

from .config import Config
from .errors import DdError
from .http import HttpClient
from .ledger import Ledger
from .models import (
    BLOCKED,
    NEEDS_KEY,
    PARTIAL,
    UNVERIFIED,
    VERIFIED,
    Evidence,
    Report,
    RiskSignal,
    SourceStatus,
    Subject,
    now_iso,
)
from .rules import SweepScope, evaluate, load_rules
from .sources.base import Source
from .sources.cninfo import CninfoSource
from .sources.eastmoney import FIELD_LABELS, EastmoneySource
from .sources.keyed import DevnorsSource, OpenCorporatesSource
from .sources.official_cn import OfficialCnSource
from .sources.registry import Registry
from .sources.sec_edgar import SecEdgarSource
from .sources.tyc_cli import TycCliSource, rule_hits as tyc_rule_hits
from .sources.wikidata import WikidataSource

CN_LISTED_CATEGORIES = {"A股", "港股"}


class Engine:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.http = HttpClient(self.config)
        self.registry = Registry(self.config, self.http)
        self.rules_doc = load_rules()

    # ---------------------------------------------------------------- 解析
    def resolve(self, name: str) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        cninfo = self.registry.get("cninfo")
        try:
            for c in cninfo.resolve(name):
                c["kind"] = "cninfo_listed"
                c["secucode"] = CninfoSource.secucode(c["code"], c.get("category") or "A股")
                candidates.append(c)
        except Exception as exc:  # noqa: BLE001
            errors.append(_err(exc))

        sec = self.registry.get("sec_edgar")
        try:
            for c in sec.resolve(name, limit=3):
                c["kind"] = "sec_edgar"
                candidates.append(c)
        except Exception as exc:  # noqa: BLE001
            errors.append(_err(exc))

        wd = self.registry.get("wikidata")
        try:
            for c in wd.resolve(name, limit=3, language="zh"):
                c["kind"] = "wikidata"
                candidates.append(c)
            if not any(c["kind"] == "wikidata" for c in candidates):
                for c in wd.resolve(name, limit=2, language="en"):
                    c["kind"] = "wikidata"
                    candidates.append(c)
        except Exception as exc:  # noqa: BLE001
            errors.append(_err(exc))

        oc = self.registry.get("opencorporates")
        if isinstance(oc, OpenCorporatesSource) and oc.key_present():
            try:
                for c in oc.search(name, limit=3).get("items", []):
                    c["kind"] = "opencorporates"
                    candidates.append(c)
            except Exception as exc:  # noqa: BLE001
                errors.append(_err(exc))

        candidates = _rank(candidates, name)
        return {
            "query": name,
            "candidates": candidates,
            "errors": errors,
            "guidance": (
                "工商/司法的权威维度在中国大陆需要官方渠道或商业数据源；"
                "本工具把「查到什么」和「查不了什么」分开列。"
            ),
        }

    # ---------------------------------------------------------------- 采集
    def build_report(
        self,
        name: str,
        *,
        announcement_pages: int = 4,
        page_size: int = 30,
        window_days: int = 180,
        include_cn_official_probe: bool = True,
    ) -> Report:
        ledger_holder: dict[str, Ledger] = {}
        subject = Subject(query=name)
        report = Report(subject=subject)
        ledger_holder["ledger"] = Ledger(self.config.home, report.report_id)

        # 1) 源可用性
        statuses = self.registry.probes()
        report.sources = statuses
        status_idx = {s.source_id: s for s in statuses}

        # 2) 消歧
        resolution = self.resolve(name)
        candidates = resolution["candidates"]
        picked = _pick(candidates)
        self._fill_subject(subject, name, picked, candidates)

        # 3) 分辖区采集
        announcements: list[dict[str, Any]] = []
        sweep = SweepScope()
        financials: dict[str, Any] = {}
        filings: list[dict[str, Any]] = []
        keyed_hits: dict[str, Any] = {}

        if subject.jurisdiction == "CN" and picked and picked.get("kind") == "cninfo_listed":
            announcements, sweep = self._collect_cn_listed(
                report, subject, picked, status_idx, announcement_pages, page_size, window_days
            )
            financials = self._collect_cn_finance(report, subject, picked)
        elif subject.jurisdiction == "US" and picked and picked.get("cik"):
            filings, financials = self._collect_us(report, subject, int(picked["cik"]))
        else:
            self._collect_identity_only(report, subject, picked)

        # Wikidata 身份信息（所有辖区都做）
        if picked and picked.get("kind") == "wikidata" and picked.get("qid"):
            self._collect_wikidata(report, picked["qid"])

        # Key 源（有 Key 才动，缺 Key 时状态已记录）
        self._collect_keyed(report, subject, status_idx, keyed_hits)

        # 4) 信号
        report.signals.extend(self._signals_from_announcements(announcements))
        report.signals.extend(self._signals_from_financials(financials))

        # 5) 规则
        results, verdict, reason = evaluate(
            report,
            announcements=announcements,
            sweep=sweep,
            financials=financials,
            filings=filings,
            keyed_hits=keyed_hits,
            rules_doc=self.rules_doc,
            jurisdiction=subject.jurisdiction,
        )
        report.rules = results
        report.verdict = verdict
        report.verdict_reason = reason

        # 6) 补证据哈希 + 人工步骤
        self._attach_ledger(report, ledger_holder["ledger"], announcements, filings)
        report.manual_steps = self._manual_steps(subject, status_idx, report)
        report.notes.append(
            "工具只做证据收集与规则判定，不替代法律、投资或雇佣决策；"
            "同名主体未消歧时红线一律按未验证处理。"
        )
        return report

    # ------------------------------------------------------------ 各辖区实现
    def _collect_cn_listed(
        self,
        report: Report,
        subject: Subject,
        picked: dict[str, Any],
        status_idx: dict[str, SourceStatus],
        pages: int,
        page_size: int,
        window_days: int = 180,
    ) -> tuple[list[dict[str, Any]], SweepScope]:
        ledger = Ledger(self.config.home, report.report_id)
        ledger_holder = {"l": ledger}
        em: EastmoneySource = self.registry.get("eastmoney")  # type: ignore[assignment]
        cn: CninfoSource = self.registry.get("cninfo")  # type: ignore[assignment]
        secucode = picked["secucode"]
        subject.ids["secucode"] = secucode
        subject.listing = f"{picked.get('category')} {picked.get('code')}"

        # 工商照面字段（聚合商 → partial）
        try:
            info = em.basic_orginfo(secucode)
            if info.get("found"):
                url = f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/Index?type=web&code={secucode.split('.')[0]}"
                payload = json.dumps(info["raw"], ensure_ascii=False)
                ev = ledger_holder["l"].add(
                    source_id="eastmoney",
                    source_label="东方财富 F10 数据中心",
                    url=url,
                    payload=payload,
                    pointer=f"RPT_F10_BASIC_ORGINFO filter SECUCODE={secucode}",
                )
                for key, val in info["fields"].items():
                    report.put(
                        key,
                        FIELD_LABELS.get(key, key),
                        val,
                        status=PARTIAL,
                        evidence=[ev],
                        note="第三方聚合字段：与公告/官方渠道一致才算核实。",
                    )
                if info["fields"].get("REG_NUM"):
                    subject.ids["credit_code"] = info["fields"]["REG_NUM"]
                subject.confidence = "high"
                subject.name = info["fields"].get("ORG_NAME") or subject.name
            else:
                report.notes.append(f"东财未返回 {secucode} 的工商照面字段。")
        except DdError as exc:
            report.sources.append(_status_from_error(self.registry.get("eastmoney"), exc))
            report.notes.append(f"工商字段采集失败：{exc.message}")

        # 股东
        try:
            sh = em.shareholders(secucode, limit=10)
            if sh["items"]:
                payload = json.dumps(sh["raw"], ensure_ascii=False)
                ev = ledger_holder["l"].add(
                    source_id="eastmoney",
                    source_label="东方财富 F10 数据中心",
                    url=f"https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/Index?type=web&code={secucode.split('.')[0]}",
                    payload=payload,
                    pointer="RPT_F10_EH_HOLDERS",
                )
                top = "；".join(
                    f"{i['rank']}. {i['name']}({i['hold_ratio']}%)"
                    for i in sh["items"][:5]
                    if i.get("name")
                )
                report.put(
                    "top_shareholders", "前五大股东", top, status=PARTIAL, evidence=[ev],
                    note="第三方聚合，股东名单以定期报告为准。",
                )
        except DdError as exc:
            report.notes.append(f"股东信息采集失败：{exc.message}")

        # 公告扫描：分页直到覆盖窗口或翻页数用尽
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
        sweep = SweepScope(
            source_id="cninfo",
            source_label="巨潮资讯网（官方披露渠道）",
            url="https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            ok=False,
            window_days=window_days,
        )
        collected: list[dict[str, Any]] = []
        effective_size = min(page_size, 30)  # cninfo 该接口单页上限实测为 30
        try:
            for page in range(1, pages + 1):
                data = cn.announcements(
                    code=picked["code"],
                    org_id=picked["org_id"],
                    column=("hke" if picked.get("category") == "港股" else "szse"),
                    page=page,
                    page_size=effective_size,
                )
                items = data["items"]
                collected.extend(_norm_ann(x) for x in items)
                oldest = min((a.get("date") or "9999") for a in collected) if collected else "9999"
                if len(items) < effective_size or oldest <= cutoff:
                    break
            dates = [a.get("date") for a in collected if a.get("date")]
            sweep.scanned = len(collected)
            sweep.ok = bool(collected)
            sweep.date_from = min(dates) if dates else ""
            sweep.date_to = max(dates) if dates else ""
            sweep.covers_window = bool(sweep.date_from and sweep.date_from <= cutoff)
            sweep.detail = sweep.scope_text() + (
                f"，已覆盖 {window_days} 天窗口" if sweep.covers_window
                else f"，未覆盖到 {cutoff}，仅作参考"
            )
            if collected:
                ev = ledger_holder["l"].add(
                    source_id="cninfo",
                    source_label="巨潮资讯网（官方披露渠道）",
                    url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
                    payload=json.dumps(collected, ensure_ascii=False),
                    pointer=f"hisAnnouncement/query stock={picked['code']},{picked['org_id']}",
                )
                report.put(
                    "announcement_sweep",
                    "公告扫描范围",
                    sweep.detail,
                    status=VERIFIED,
                    evidence=[ev],
                    note="",
                )
        except DdError as exc:
            sweep.ok = False
            sweep.detail = exc.message
            report.put(
                "announcement_sweep", "公告扫描范围", None, status=BLOCKED,
                note=f"公告渠道不可用：{exc.message}",
            )
        return collected, sweep

    def _collect_cn_finance(
        self, report: Report, subject: Subject, picked: dict[str, Any]
    ) -> dict[str, Any]:
        em: EastmoneySource = self.registry.get("eastmoney")  # type: ignore[assignment]
        secucode = picked["secucode"]
        try:
            fin = em.main_finance(secucode, limit=4)
            if not fin["items"]:
                report.put("main_finance", "主要财务指标", None, status=UNVERIFIED,
                           note="财务接口返回空。")
                return {}
            ledger = Ledger(self.config.home, report.report_id)
            ev = ledger.add(
                source_id="eastmoney",
                source_label="东方财富 F10 数据中心",
                url=f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?type=web&code={secucode.split('.')[0]}",
                payload=json.dumps(fin["raw"], ensure_ascii=False),
                pointer="RPT_F10_FINANCE_MAINFINADATA",
            )
            latest = fin["items"][0]
            report.put(
                "main_finance",
                "最新报告期主要财务指标",
                json.dumps(latest, ensure_ascii=False),
                status=PARTIAL,
                evidence=[ev],
                note="第三方聚合口径，且需按行业基准解读。",
            )
            return {"eastmoney": {"status": "ok", "latest": latest, "items": fin["items"]}}
        except DdError as exc:
            report.put("main_finance", "主要财务指标", None, status=BLOCKED,
                       note=f"财务接口不可用：{exc.message}")
            return {}

    def _collect_us(
        self, report: Report, subject: Subject, cik: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        sec: SecEdgarSource = self.registry.get("sec_edgar")  # type: ignore[assignment]
        ledger = Ledger(self.config.home, report.report_id)
        filings: list[dict[str, Any]] = []
        financials: dict[str, Any] = {}
        try:
            prof = sec.profile(cik)
            ev = ledger.add(
                source_id="sec_edgar",
                source_label="SEC EDGAR（官方）",
                url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                payload=json.dumps(prof, ensure_ascii=False),
                pointer="submissions",
            )
            subject.name = prof["name"]
            subject.ids["cik"] = str(cik)
            if prof.get("tickers"):
                subject.ids["ticker"] = ", ".join(prof["tickers"])
            if prof.get("lei"):
                subject.ids["lei"] = prof["lei"]
            subject.confidence = "high"
            for key, label in (
                ("name", "企业名称"), ("tickers", "股票代码"), ("exchanges", "交易市场"),
                ("sic_description", "行业(SIC)"), ("ein", "EIN(雇主识别号)"),
                ("state_of_incorporation", "注册州"), ("fiscal_year_end", "财年结束日"),
                ("address", "注册地址"), ("former_names", "曾用名"),
            ):
                val = prof.get(key)
                if val in (None, "", [], 0):
                    continue
                report.put(key, label, val, status=VERIFIED, evidence=[ev])
        except DdError as exc:
            report.notes.append(f"SEC 主体信息失败：{exc.message}")

        try:
            fl = sec.filings(cik, limit=40)
            filings = fl["items"]
            ev = ledger.add(
                source_id="sec_edgar",
                source_label="SEC EDGAR（官方）",
                url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}&type=&dateb=&owner=include&count=40",
                payload=json.dumps(filings, ensure_ascii=False),
                pointer="filings.recent",
            )
            report.put(
                "recent_filings", "近期申报",
                "；".join(f"{f['filing_date']} {f['form']}" for f in filings[:10]),
                status=VERIFIED, evidence=[ev],
            )
        except DdError as exc:
            report.put("recent_filings", "近期申报", None, status=BLOCKED,
                       note=f"申报清单不可用：{exc.message}")

        try:
            financials = sec.financials(cik)
            ok = {k: v for k, v in financials.items() if v.get("status") == "ok"}
            if ok:
                ev = ledger.add(
                    source_id="sec_edgar",
                    source_label="SEC EDGAR（官方 XBRL）",
                    url=f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}",
                    payload=json.dumps(financials, ensure_ascii=False),
                    pointer="companyconcept",
                )
                summary = "；".join(
                    f"{v['label']}={v['latest'][0]['value']}（{v['latest'][0]['end']}）"
                    for v in ok.values() if v.get("latest")
                )
                report.put("financials", "主要财务口径", summary, status=VERIFIED, evidence=[ev])
        except DdError as exc:
            report.notes.append(f"财务口径采集失败：{exc.message}")
        return filings, financials

    def _collect_identity_only(
        self, report: Report, subject: Subject, picked: dict[str, Any] | None
    ) -> None:
        report.notes.append(
            "未在上市公司官方渠道匹配到主体：该主体可能是非上市/境外企业，"
            "工商与司法维度需要官方渠道人工核查或配置商业数据源 Key。"
        )
        if picked and picked.get("kind") == "wikidata" and picked.get("qid"):
            subject.confidence = "low"
            subject.name = picked.get("name") or subject.name

    def _collect_wikidata(self, report: Report, qid: str) -> None:
        wd: WikidataSource = self.registry.get("wikidata")  # type: ignore[assignment]
        ledger = Ledger(self.config.home, report.report_id)
        try:
            ent = wd.entity(qid)
            ev = ledger.add(
                source_id="wikidata",
                source_label="Wikidata（CC0）",
                url=ent["url"],
                payload=json.dumps(ent["raw"], ensure_ascii=False),
                pointer="wbgetentities",
            )
            facts = ent.get("facts") or {}
            mapping = {
                "lei": ("lei", "LEI"), "country": ("country", "国家/地区"),
                "inception": ("inception", "成立时间"), "parent": ("parent", "母公司"),
                "hq": ("hq", "总部"), "employees": ("employees", "员工数"),
                "website": ("website", "官网"), "ticker": ("ticker", "股票代码"),
                "isin": ("isin", "ISIN"), "ceo": ("ceo", "CEO"),
                "legal_form": ("legal_form", "法律形式"),
            }
            for k, (nk, label) in mapping.items():
                if k in facts:
                    vals = facts[k]["values"]
                    report.put(
                        nk, label, "；".join(str(v) for v in vals[:5]) if isinstance(vals, list) else vals,
                        status=PARTIAL, evidence=[ev],
                        note="知识库数据，可能滞后；仅作身份线索。",
                    )
            report.subject.ids.setdefault("qid", qid)
        except DdError as exc:
            report.notes.append(f"Wikidata 实体读取失败：{exc.message}")

    def _collect_keyed(
        self,
        report: Report,
        subject: Subject,
        status_idx: dict[str, SourceStatus],
        keyed_hits: dict[str, Any],
    ) -> None:
        dev = self.registry.get("devnors")
        if isinstance(dev, DevnorsSource) and dev.key_present() and subject.name:
            ledger = Ledger(self.config.home, report.report_id)
            try:
                res = dev.query("enterprise", "company_detail_v2", subject.name)
                hits = res.get("hits") or []
                if hits:
                    ev = ledger.add(
                        source_id="devnors",
                        source_label=dev.spec.label,
                        url="https://data.devnors.com/v1/data/query",
                        payload=json.dumps(res, ensure_ascii=False),
                        pointer="enterprise/company_detail_v2",
                    )
                    first = hits[0]
                    for k, v in list(first.items())[:15]:
                        report.put(
                            f"devnors_{k}", f"[聚合]{k}", v, status=PARTIAL, evidence=[ev],
                            note="第三方聚合字段，来源授权口径未公开；须与官方渠道交叉验证。",
                        )
                if hits:
                    rl01 = keyed_hits.setdefault(
                        "RL-01",
                        {
                            "source": "devnors",
                            "source_label": dev.spec.label,
                            "url": dev.spec.homepage,
                            "checked": [],
                            "labels": [],
                            "evidence": [],
                        },
                    )
                    subject_id = first.get("id")
                    for tool, label in (("exec_person", "被执行人"), ("breach_of_trust", "失信被执行人")):
                        try:
                            r2 = dev.query("enterprise", tool, "", filters={"id": subject_id})
                            rl01["checked"].append(label)
                            rl01["labels"].append(label)
                            for item in (r2.get("hits") or [])[:3]:
                                rl01["evidence"].append({"tool": tool, "label": label, "item": item})
                        except DdError:
                            continue
                    if not rl01["evidence"] and not rl01["checked"]:
                        keyed_hits.pop("RL-01", None)
            except DdError as exc:
                report.notes.append(f"Devnors 采集失败：{exc.message}")

        # 天眼查官方 CLI：已安装且登录才执行；每个维度独立记录，不能因一个命令失败而吞掉全局证据。
        tyc = self.registry.get("tianyancha_cli")
        if isinstance(tyc, TycCliSource) and subject.name:
            try:
                key = subject.ids.get("credit_code") or subject.name
                collected = tyc.collect_for_rules(key)
                hits = tyc_rule_hits(collected)
                for rid, value in hits.items():
                    value["source_label"] = tyc.spec.label
                    value["url"] = tyc.spec.homepage
                    keyed_hits[rid] = value
                ok_tools = [v["label"] for v in collected.values() if v.get("status") == "ok"]
                bad_tools = [f"{v['label']}：{v.get('error', v.get('status'))}" for v in collected.values() if v.get("status") != "ok"]
                report.notes.append(
                    f"天眼查 CLI 已调用 {len(ok_tools)} 个维度" + (f"（{', '.join(ok_tools)}）" if ok_tools else "") + "."
                )
                if bad_tools:
                    report.notes.append("天眼查 CLI 部分维度失败：" + "；".join(bad_tools))
            except DdError as exc:
                report.notes.append(f"天眼查 CLI 采集失败：{exc.message}")

        for sid in ("opencorporates", "tianyancha_cli"):
            st = status_idx.get(sid)
            if st and st.status == "needs_key":
                report.put(
                    f"keyed_{sid}", f"[需 Key]{st.label}", None, status=NEEDS_KEY,
                    note=st.detail,
                )

    # ---------------------------------------------------------------- 工具
    def _fill_subject(
        self,
        subject: Subject,
        query: str,
        picked: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
    ) -> None:
        subject.query = query
        if not picked:
            subject.name = query
            subject.confidence = "low"
            subject.disambiguation = "没有匹配到任何主体；请提供企业全称或统一社会信用代码。"
            return
        subject.name = picked.get("name") or query
        if picked.get("kind") == "cninfo_listed":
            subject.jurisdiction = "CN"
            subject.ids["stock_code"] = str(picked.get("code"))
            subject.ids.setdefault("org_id", str(picked.get("org_id")))
            subject.disambiguation = (
                f"匹配到 {len(candidates)} 个候选；已选上市公司主体 {subject.name}"
                f"（{picked.get('category')} {picked.get('code')}）。"
                "非上市同名主体需另行核对统一社会信用代码。"
            )
        elif picked.get("kind") == "sec_edgar":
            subject.jurisdiction = "US"
            subject.ids["cik"] = str(picked.get("cik"))
            subject.ids["ticker"] = str(picked.get("ticker"))
            subject.disambiguation = "SEC 登记主体（美国上市公司）。"
        else:
            subject.jurisdiction = "GLOBAL"
            subject.ids["qid"] = str(picked.get("qid") or "")
            subject.disambiguation = (
                "仅匹配到知识库实体，未匹配到官方登记渠道；"
                "工商与司法维度需人工核查或配置商业数据源。"
            )

    def _signals_from_announcements(self, announcements: list[dict[str, Any]]) -> list[RiskSignal]:
        out: list[RiskSignal] = []
        for rule in self.rules_doc["rules"]:
            patterns = rule.get("patterns") or []
            if not patterns:
                continue
            import re

            regex = re.compile("|".join(re.escape(p) for p in patterns), re.IGNORECASE)
            for ann in announcements:
                if regex.search(ann.get("title") or ""):
                    out.append(
                        RiskSignal(
                            level="red" if rule["severity"] == "red_line" else "yellow",
                            title=f"[{rule['id']}] {ann.get('title')}",
                            detail=f"命中规则「{rule['title']}」的关键词。",
                            source_id="cninfo",
                            evidence_url=ann.get("url", ""),
                            date=ann.get("date", ""),
                        )
                    )
        return out

    def _signals_from_financials(self, financials: dict[str, Any]) -> list[RiskSignal]:
        out: list[RiskSignal] = []
        latest = ((financials.get("eastmoney") or {}).get("latest") or {})
        if not latest:
            return out
        try:
            ratio = float(latest.get("DEBT_ASSET_RATIO")) if latest.get("DEBT_ASSET_RATIO") else None
        except (TypeError, ValueError):
            ratio = None
        if ratio is not None and ratio > 80:
            out.append(
                RiskSignal(
                    level="yellow",
                    title=f"资产负债率 {ratio:.2f}% 偏高",
                    detail="超过 80% 的负债率会放大再融资与现金流压力；需结合行业基准判断。",
                    source_id="eastmoney",
                    evidence_url="https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index",
                )
            )
        net = latest.get("PARENT_NETPROFIT")
        try:
            if net is not None and float(net) < 0:
                out.append(
                    RiskSignal(
                        level="yellow",
                        title=f"最新报告期归母净利润为负（{net}）",
                        detail="亏损会直接影响奖金兑现与裁员概率，需看是否连续。",
                        source_id="eastmoney",
                        evidence_url="https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index",
                    )
                )
        except (TypeError, ValueError):
            pass
        return out

    def _attach_ledger(
        self,
        report: Report,
        ledger: Ledger,
        announcements: list[dict[str, Any]],
        filings: list[dict[str, Any]],
    ) -> None:
        by_url = {a.get("url"): a for a in announcements if a.get("url")}
        filings_by_url = {f.get("url"): f for f in filings if f.get("url")}
        for rule in report.rules:
            for ev in rule.evidence:
                if ev.sha256:
                    continue
                payload = None
                if ev.url in by_url:
                    payload = json.dumps(by_url[ev.url], ensure_ascii=False)
                elif ev.url in filings_by_url:
                    payload = json.dumps(filings_by_url[ev.url], ensure_ascii=False)
                if payload is None and (ev.quote or ev.pointer):
                    # Key 源/人工证据没有公告 URL 可反查时，至少把已提取的载荷摘要落台账，
                    # 保证每条 hit 证据都有可复核的 sha256，而不是空哈希。
                    payload = json.dumps(
                        {"pointer": ev.pointer, "quote": ev.quote}, ensure_ascii=False
                    )
                if payload is None:
                    continue
                new = ledger.add(
                    source_id=ev.source_id,
                    source_label=ev.source_label,
                    url=ev.url,
                    payload=payload,
                    pointer=ev.pointer,
                    quote=ev.quote,
                )
                ev.sha256 = new.sha256
                ev.raw_path = new.raw_path

    def _manual_steps(
        self, subject: Subject, status_idx: dict[str, SourceStatus], report: Report
    ) -> list[str]:
        steps: list[str] = []
        if subject.jurisdiction in ("CN", "unknown"):
            steps.extend(OfficialCnSource.manual_plan(subject.name))
        for sid, st in status_idx.items():
            if st.status == "needs_key":
                steps.append(f"[配置] {st.label}：{st.detail}")
        if not report.fields:
            steps.append("[兜底] 未采集到任何字段：先确认企业全称或统一社会信用代码，再重跑。")
        return steps


# ------------------------------------------------------------------ 辅助函数
def _norm_ann(item: dict[str, Any]) -> dict[str, Any]:
    ts = item.get("time_ms")
    date = ""
    if isinstance(ts, (int, float)) and ts > 0:
        from datetime import datetime, timedelta, timezone

        date = datetime.fromtimestamp(
            ts / 1000, tz=timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d")
    return {
        "title": item.get("title", ""),
        "date": date,
        "url": item.get("url", ""),
        "code": item.get("code"),
        "name": item.get("name"),
    }


def _norm_match_text(value: Any) -> str:
    import re

    return re.sub(r"[\s\-_.()（）,，]+", "", str(value or "")).lower()


def _reliable_match(candidate: dict[str, Any], query: str) -> bool:
    """只接受能指回同一主体的匹配，避免把模糊搜索第一名误当调查对象。"""
    q = _norm_match_text(query)
    if not q:
        return False
    name = _norm_match_text(candidate.get("name"))
    if name and (name == q or q in name or name in q):
        return True
    for key in ("code", "ticker", "cik", "qid", "credit_code"):
        anchor = _norm_match_text(candidate.get(key))
        if anchor and anchor == q:
            return True
    return False


def _rank(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    order = {"cninfo_listed": 0, "sec_edgar": 1, "opencorporates": 2, "wikidata": 3}
    q = query.strip()
    for c in candidates:
        c["exact_name_match"] = (c.get("name") or "").strip() == q
        c["reliable_match"] = _reliable_match(c, q)
    return sorted(
        candidates,
        key=lambda c: (
            0 if c.get("reliable_match") else 1,
            order.get(c.get("kind", "wikidata"), 9),
            0 if c.get("exact_name_match") else 1,
        ),
    )


def _pick(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    reliable = [c for c in candidates if c.get("reliable_match")]
    for c in reliable:
        if c.get("kind") == "cninfo_listed" and c.get("category") in CN_LISTED_CATEGORIES:
            return c
    for c in reliable:
        if c.get("kind") == "sec_edgar":
            return c
    for c in reliable:
        if c.get("kind") == "opencorporates":
            return c
    return reliable[0] if reliable else None


def _err(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DdError):
        return exc.to_dict()
    return {"status": "error", "message": str(exc)[:200]}


def _status_from_error(src: Source, exc: Exception) -> SourceStatus:
    if isinstance(exc, DdError):
        return src.failed(exc.url or src.spec.homepage, exc)
    return src.failed(src.spec.homepage, exc)
