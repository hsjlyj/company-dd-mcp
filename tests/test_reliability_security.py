"""可靠性与安全回归：默认配置可运行、证据哈希可复核、报告 ID 不能穿越路径。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from company_dd.config import Config
from company_dd.ledger import Ledger, ReportStore
from company_dd.manual import ManualFinding, ManualStore, reevaluate_report
from company_dd.models import Report, Subject
from company_dd.collect import _pick, _rank
from company_dd.rules import evaluate
from company_dd.sources.sec_edgar import SecEdgarSource
from company_dd.http import HttpClient


def test_config_defaults_do_not_require_opt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("COMPANY_DD_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg = Config()

    assert cfg.home == tmp_path / ".company-dd"
    assert (cfg.home / "reports").is_dir()


def test_config_reads_numeric_env_at_instance_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COMPANY_DD_HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_DD_TIMEOUT", "7.5")
    monkeypatch.setenv("COMPANY_DD_RETRIES", "4")
    monkeypatch.setenv("COMPANY_DD_HOST_INTERVAL", "0.1")

    cfg = Config()

    assert cfg.timeout == 7.5
    assert cfg.max_retries == 4
    assert cfg.min_host_interval == 0.1


def test_config_invalid_numeric_env_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COMPANY_DD_HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_DD_TIMEOUT", "not-a-number")

    assert Config().timeout == 20.0


def test_ledger_hash_matches_complete_payload(tmp_path: Path) -> None:
    payload = "x" * (2_000_001)
    ev = Ledger(tmp_path, "r1").add(
        source_id="source/with unsafe chars",
        source_label="测试源",
        url="https://example.test",
        payload=payload,
    )

    raw = Path(ev.raw_path).read_bytes()

    assert len(raw) == len(payload.encode("utf-8"))
    assert hashlib.sha256(raw).hexdigest() == ev.sha256
    assert "/" not in Path(ev.raw_path).name


def test_report_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = ReportStore(tmp_path)

    with pytest.raises(ValueError, match="非法报告编号"):
        store.load("../outside")


def test_report_store_save_rejects_unsafe_report_id(tmp_path: Path) -> None:
    report = Report(subject=Subject(query="x"), report_id="../outside")

    with pytest.raises(ValueError, match="非法报告编号"):
        ReportStore(tmp_path).save(report)


def test_manual_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = ManualStore(tmp_path)
    finding = ManualFinding(
        rule_id="RL-01",
        outcome="clear",
        operator="u",
        method="GSXT",
        url="https://example.test",
    )

    with pytest.raises(ValueError, match="非法报告编号"):
        store.add("../outside", finding)


def test_pick_accepts_name_abbreviation_and_anchors() -> None:
    cn = _rank([
        {"kind": "cninfo_listed", "name": "宁德时代新能源科技股份有限公司", "category": "A股", "code": "300750"}
    ], "宁德时代")
    assert _pick(cn)["code"] == "300750"

    by_code = _rank([
        {"kind": "cninfo_listed", "name": "宁德时代新能源科技股份有限公司", "category": "A股", "code": "300750"}
    ], "300750")
    assert _pick(by_code)["name"] == "宁德时代新能源科技股份有限公司"


def test_pick_rejects_unrelated_fuzzy_first_result() -> None:
    candidates = _rank(
        [{"kind": "cninfo_listed", "name": "中国平安保险（集团）股份有限公司", "category": "A股", "code": "601318"}],
        "Ping An Insurance",
    )

    assert _pick(candidates) is None


def test_keyed_slot_without_checked_scope_is_unverified() -> None:
    report = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    rules, _, _ = evaluate(
        report,
        keyed_hits={"RL-01": {"source": "devnors", "source_label": "Devnors", "evidence": []}},
    )

    rl01 = next(r for r in rules if r.rule_id == "RL-01")
    assert rl01.status == "unverified"
    assert "未返回可核验范围" in rl01.reason


def test_keyed_rule_hit_always_carries_evidence() -> None:
    report = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    rules, verdict, _ = evaluate(
        report,
        keyed_hits={
            "RL-01": {
                "source": "devnors",
                "source_label": "Devnors Data",
                "url": "https://data.devnors.com/",
                "checked": ["被执行人"],
                "labels": ["被执行人"],
                "evidence": [
                    {"tool": "exec_person", "label": "被执行人", "item": {"case": "A-1"}}
                ],
            }
        },
    )

    rl01 = next(r for r in rules if r.rule_id == "RL-01")

    assert rl01.status == "hit"
    assert rl01.evidence
    assert "A-1" in rl01.evidence[0].quote
    assert verdict == "red"


def test_manual_clear_does_not_erase_machine_hit(tmp_path: Path) -> None:
    report = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    rules, _, _ = evaluate(
        report,
        keyed_hits={
            "RL-01": {
                "source": "devnors",
                "source_label": "Devnors Data",
                "checked": ["被执行人"],
                "evidence": [{"tool": "exec_person", "label": "被执行人", "item": {"case": "A-1"}}],
            }
        },
    )
    data = report.to_dict()
    data["rules"] = [r.to_dict() for r in rules]
    store = ManualStore(tmp_path)
    store.add(
        "r1",
        ManualFinding(
            rule_id="RL-01", outcome="clear", operator="u", method="人工复核",
            url="https://example.test", note="同名主体，非本公司",
        ),
    )

    updated = reevaluate_report(data, store.load("r1"))
    rl01 = next(r for r in updated["rules"] if r["rule_id"] == "RL-01")

    assert rl01["status"] == "hit"
    assert "存在冲突" in rl01["reason"]
    assert len(rl01["evidence"]) == 2
    assert updated["verdict"] == "red"
    assert updated["evidence_count"] == 2


def test_manual_store_corruption_fails_closed(tmp_path: Path) -> None:
    store = ManualStore(tmp_path)
    store.path("r1").write_text("{broken", encoding="utf-8")

    with pytest.raises(Exception, match="人工核查记录损坏"):
        store.load("r1")

    assert store.path("r1").read_text(encoding="utf-8") == "{broken"


def test_sec_filings_tolerates_ragged_arrays(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = Config(home=tmp_path)
    src = SecEdgarSource(cfg, HttpClient(cfg))
    monkeypatch.setattr(src, "_json", lambda url: {
        "filings": {
            "recent": {
                "form": ["10-K", "8-K", "10-Q"],
                "filingDate": ["2026-01-01"],
                "accessionNumber": ["0001"],
                "primaryDocument": [],
            }
        }
    })

    out = src.filings(320193)

    assert len(out["items"]) == 3
    assert out["items"][1]["filing_date"] == ""
