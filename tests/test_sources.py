"""数据源层测试：不打真实网络，验证解析/映射/拦截语义。"""

from __future__ import annotations

import pytest

from company_dd.config import Config
from company_dd.http import HttpClient
from company_dd.sources.cninfo import CninfoSource
from company_dd.sources.official_cn import OfficialCnSource
from company_dd.sources.sec_edgar import SecEdgarSource


def test_secucode_mapping_covers_cn_markets() -> None:
    assert CninfoSource.secucode("600519", "A股") == "600519.SH"
    assert CninfoSource.secucode("300750", "A股") == "300750.SZ"
    assert CninfoSource.secucode("000001", "A股") == "000001.SZ"
    # 实测：43/83/87 段在东财是 .NQ，用 .BJ 查出来是空；920 段才是 .BJ
    assert CninfoSource.secucode("830799", "A股") == "830799.NQ"
    assert CninfoSource.secucode("871981", "A股") == "871981.NQ"
    assert CninfoSource.secucode("920008", "A股") == "920008.BJ"
    assert CninfoSource.secucode("03750", "港股") == "03750.HK"


def test_announcement_normalisation_keeps_source_url_and_date() -> None:
    row = {
        "announcementTitle": "<em>宁德时代</em>：关于某某的公告",
        "secCode": "300750",
        "secName": "宁德时代",
        "announcementId": "1",
        "announcementTime": 1790585048000,
        "adjunctUrl": "finalpage/2026-09-28/1225584204.PDF",
        "adjunctType": "PDF",
    }
    out = CninfoSource._norm_announcement(row)
    assert out["title"].startswith("宁德时代")
    assert out["url"].startswith("https://static.cninfo.com.cn/")
    assert out["date"].startswith("2026-")


def test_manual_plan_lists_every_official_channel() -> None:
    steps = OfficialCnSource.manual_plan("某某科技有限公司")
    joined = "\n".join(steps)
    for url in ("gsxt.gov.cn", "creditchina.gov.cn", "zxgk.court.gov.cn", "wenshu.court.gov.cn"):
        assert url in joined
    assert "统一社会信用代码" in joined
    assert "某某科技有限公司" in joined


def test_keyed_source_reports_needs_key_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCORPORATES_API_KEY", raising=False)
    cfg = Config()
    from company_dd.sources.keyed import OpenCorporatesSource

    src = OpenCorporatesSource(cfg, HttpClient(cfg))
    st = src.probe()
    assert st.status == "needs_key"
    assert "OPENCORPORATES_API_KEY" in st.detail


def test_sec_resolve_matches_ticker_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config()
    src = SecEdgarSource(cfg, HttpClient(cfg))
    fake = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
    monkeypatch.setattr(src, "_json", lambda url: fake)
    hits = src.resolve("aapl")
    assert hits and hits[0]["cik"] == 320193


def test_official_cn_probe_marks_homepage_reachable_but_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config()
    src = OfficialCnSource(cfg, HttpClient(cfg))

    class R:
        status = 200

    monkeypatch.setattr(src.http, "request", lambda *a, **k: R())
    st = src.probe_site({"id": "wenshu", "label": "中国裁判文书网", "url": "https://wenshu.court.gov.cn/"})
    assert st.status == "blocked"
    assert "验证码" in st.detail
    assert st.manual_url.startswith("https://wenshu.court.gov.cn")
