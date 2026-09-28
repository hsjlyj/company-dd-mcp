"""规则/模型/报告层的单元测试：不打网络，用构造数据验证判定语义。"""

from __future__ import annotations

import json

import pytest

from company_dd.errors import DdError, is_blocked
from company_dd.models import (
    BLOCKED,
    NEEDS_KEY,
    PARTIAL,
    VERIFIED,
    Evidence,
    Report,
    Subject,
    now_iso,
)
from company_dd.report import render_html, render_markdown
from company_dd.rules import SweepScope, evaluate, load_rules


def _ev(url: str = "https://example.com/a") -> Evidence:
    return Evidence(
        source_id="t", source_label="测试源", url=url, fetched_at=now_iso(),
        pointer="p", sha256="deadbeef", raw_path="/tmp/x",
    )


# ------------------------------------------------------------------ 模型不变式
def test_verified_field_requires_evidence() -> None:
    rep = Report(subject=Subject(query="x"))
    with pytest.raises(ValueError):
        rep.put("a", "字段A", "v", status=VERIFIED)


def test_open_status_requires_note() -> None:
    rep = Report(subject=Subject(query="x"))
    with pytest.raises(ValueError):
        rep.put("a", "字段A", None, status=BLOCKED)
    f = rep.put("b", "字段B", None, status=NEEDS_KEY, note="缺 Key")
    assert f.status == NEEDS_KEY
    assert rep.gaps()[0].name == "b"


def test_partial_is_not_a_gap_but_is_a_weak_status() -> None:
    rep = Report(subject=Subject(query="x"))
    rep.put("a", "字段A", 1, status=PARTIAL, evidence=[_ev()], note="聚合源")
    assert rep.gaps() == []


# ------------------------------------------------------------------ 规则语义
def _ann(title: str, date: str = "2026-05-01") -> dict:
    return {"title": title, "date": date, "url": "https://example.com/pdf", "code": "000001"}


def test_clear_requires_covered_window() -> None:
    rep = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    sweep_ok = SweepScope(source_id="cninfo", source_label="巨潮", scanned=120, ok=True,
                          date_from="2026-01-01", date_to="2026-06-01", window_days=180,
                          covers_window=True)
    rules, verdict, _ = evaluate(rep, announcements=[_ann("关于董事会决议的公告")], sweep=sweep_ok)
    rl3 = next(r for r in rules if r.rule_id == "RL-03")
    assert rl3.status == "clear"
    assert verdict == "yellow"  # 官方渠道维度仍然查不了


def test_narrow_window_downgrades_clear_to_unverified() -> None:
    rep = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    narrow = SweepScope(source_id="cninfo", source_label="巨潮", scanned=30, ok=True,
                        date_from="2026-08-01", date_to="2026-09-01", window_days=180,
                        covers_window=False)
    rules, _, _ = evaluate(rep, announcements=[_ann("关于董事会决议的公告")], sweep=narrow)
    rl3 = next(r for r in rules if r.rule_id == "RL-03")
    assert rl3.status == "unverified"
    assert "未达 180 天窗口" in rl3.reason


def test_hit_threshold_and_evidence() -> None:
    rep = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    sweep = SweepScope(source_id="cninfo", source_label="巨潮", scanned=100, ok=True,
                       date_from="2026-01-01", date_to="2026-06-01", window_days=180,
                       covers_window=True)
    anns = [_ann("关于收到行政处罚决定书的公告"), _ann("关于劳动仲裁结果的公告")]
    rules, verdict, reason = evaluate(rep, announcements=anns, sweep=sweep)
    rl3 = next(r for r in rules if r.rule_id == "RL-03")
    assert rl3.status == "hit" and rl3.evidence and "行政处罚" in rl3.evidence[0].quote
    assert verdict in ("red", "yellow")
    assert "RL-03" in reason or verdict == "red"


def test_verdict_red_on_red_line_hit() -> None:
    rep = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    sweep = SweepScope(source_id="cninfo", source_label="巨潮", scanned=50, ok=True,
                       date_from="2026-01-01", date_to="2026-06-01", window_days=180,
                       covers_window=True)
    rules, verdict, reason = evaluate(rep, announcements=[_ann("公司股票可能被终止上市的风险提示公告")], sweep=sweep)
    assert verdict == "red"
    assert "RL-08" in reason


def test_verdict_inconclusive_when_nothing_verified() -> None:
    rep = Report(subject=Subject(query="x", jurisdiction="GLOBAL"))
    rules, verdict, reason = evaluate(rep)
    assert verdict == "inconclusive"
    assert "未匹配到任何可核验的官方披露渠道" in reason
    assert all(r.status == "unverified" for r in rules)


def test_us_filings_drive_rl08_not_cn_announcements() -> None:
    rep = Report(subject=Subject(query="x", jurisdiction="US", listing="AAPL"))
    filings = [{"form": "10-K", "filing_date": "2026-01-01", "url": "https://sec.gov/a"},
               {"form": "NT 10-Q", "filing_date": "2026-02-01", "url": "https://sec.gov/b"}]
    rules, verdict, _ = evaluate(rep, filings=filings, jurisdiction="US")
    rl8 = next(r for r in rules if r.rule_id == "RL-08")
    assert rl8.status == "hit"
    assert rl8.evidence[0].url == "https://sec.gov/b"
    assert verdict == "red"


def test_green_requires_all_red_lines_verified() -> None:
    """构造一个全部红线都能核验且未命中的场景（Key 源齐全时的目标形态）。"""
    doc = {"rules": [dict(r, scope="financials") for r in load_rules()["rules"]]}
    rep = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    rules, verdict, _ = evaluate(rep, financials={"x": {"status": "ok"}}, rules_doc=doc)
    assert all(r.status == "clear" for r in rules)
    assert verdict == "green"


# ------------------------------------------------------------------ 报告渲染
def test_markdown_has_gap_and_source_sections() -> None:
    rep = Report(subject=Subject(query="宁德时代", name="宁德时代新能源科技股份有限公司"))
    rep.put("announcement_sweep", "公告扫描范围", "120 条", status=VERIFIED, evidence=[_ev()])
    rep.put("keyed_x", "[需 Key]", None, status=NEEDS_KEY, note="缺 DEVNORS_API_KEY")
    from company_dd.models import SourceStatus

    rep.sources = [SourceStatus(source_id="gsxt", label="国家企业信用信息公示系统", kind="official",
                                jurisdiction="CN", status="blocked", http_status=521,
                                detail="WAF 拦截")]
    md = render_markdown(rep.to_dict())
    assert "未验证清单" in md
    assert "数据源状态" in md
    assert "521" in md
    assert "缺 DEVNORS_API_KEY" in md
    rep.verdict = "yellow"
    html = render_html(rep.to_dict())
    assert "红线判定" in html
    assert "#ef6c00" in html  # yellow 结论色
    assert "未验证清单" in html


# ------------------------------------------------------------------ 错误与拦截识别
def test_is_blocked_detects_waf_and_captcha() -> None:
    assert is_blocked(521)
    assert is_blocked(412)
    assert is_blocked(200, "请输入验证码")
    assert not is_blocked(200, '{"ok":true}')


def test_blocked_error_carries_manual_url() -> None:
    from company_dd.errors import blocked_error

    err = blocked_error("gsxt", "国家企业信用信息公示系统", "https://www.gsxt.gov.cn/", 521)
    d = err.to_dict()
    assert d["code"] == "blocked" and d["http_status"] == 521
    assert d["manual_url"].startswith("https://www.gsxt.gov.cn")


def test_rules_json_is_valid_and_complete() -> None:
    doc = load_rules()
    ids = [r["id"] for r in doc["rules"]]
    assert len(ids) == len(set(ids))
    assert any(r["severity"] == "red_line" for r in doc["rules"])
    for r in doc["rules"]:
        assert r.get("title") and r.get("expectation") and r.get("why")
