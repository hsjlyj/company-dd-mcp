"""可靠性与安全回归：默认配置可运行、证据哈希可复核、报告 ID 不能穿越路径。"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
import zipfile
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


# ---------------------------------------------------------------------------
# install.sh 的可执行位与内容断言
#
# 这一组断言的存在理由（写下来，别让下一个人重新踩）：
#
#   * install.sh 与 tools/refresh_data_sources.py 在 main 上丢过可执行位
#     （100644），`./install.sh` 实测 exit 126。此前没有任何 CI 断言在看它，
#     掉了两次（f475c8e / 7fed6cc）都靠人肉发现。
#   * 7fed6cc 那笔提交信息是 "restore executable bit"（声称只改权限位），
#     实际把 install.sh 从 31 行的 ef5e31f 整份换成了 50 行的 34e769f，
#     而且 mode 最后仍是 100644 —— 声明的事没做成，没声明的事做了一件。
#
# install.sh 在仓库里存在过三个版本，内容断言因此不能用固定片段：
#
#   blob      行数  安装方式
#   52bb33d   30    手抄依赖 pip install -q mcp httpx anyio pytest（装不上项目本身）
#   ef5e31f   31    "$ROOT/.venv/bin/pip" install -q -e ".[dev]"
#   34e769f   50    "$VENV_PY" -m pip install -e ".[dev]"
#
# 全组曾用大致的 grep 模式判过这个文件至少五次，全部是假阴性
# （例如 `grep 'pip install'`：pip 与 install 之间实际隔着 `" `）。
# 所以这里断言**语义**（做了 editable 安装、没退回手抄依赖），
# 不断言**写法**（参数顺序、变量名、行数都不管）。
# ---------------------------------------------------------------------------

# editable 安装：install 与 -e ".[dev]" 之间允许有任何参数。
_EDITABLE_INSTALL = re.compile(r'install\b[^\n]*\s-e\s+"?\.\[dev\]"?')

# 初版的手抄依赖写法：能装依赖，但装不上项目本身。
_HAND_COPIED_DEPS = "mcp httpx anyio pytest"


def _install_sh_text(ref: str = "HEAD") -> str:
    """返回 install.sh 在指定 git ref 的完整文本。"""
    result = subprocess.run(
        ["git", "show", f"{ref}:install.sh"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_install_sh_mode() -> None:
    """install.sh 必须是 100755（可执行）。这是变更检测器，不是正确性判据。

    有人把它改成不可执行 → CI 红，这是对的（用户 ./install.sh 会 exit 126）。
    有人有意改成不可执行 → 更新本断言的期望值并在 PR 描述里说明原因。
    """
    result = subprocess.run(
        ["git", "ls-files", "-s", "install.sh"],
        capture_output=True,
        text=True,
        check=True,
    )
    # ls-files -s 输出格式: <mode> <sha> <stage>\t<filename>
    mode = result.stdout.split()[0]
    assert mode == "100755", (
        f"install.sh 在 git index 里的 mode 是 {mode}，应为 100755。\n"
        "修法：git add --chmod=+x install.sh（连接器上传会丢 mode，"
        "必须用带凭据的环境推）。"
    )


def test_refresh_data_sources_sh_mode() -> None:
    """tools/refresh_data_sources.py 必须是 100755。"""
    result = subprocess.run(
        ["git", "ls-files", "-s", "tools/refresh_data_sources.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    mode = result.stdout.split()[0]
    assert mode == "100755", (
        f"tools/refresh_data_sources.py 的 mode 是 {mode}，应为 100755。\n"
        "修法：git add --chmod=+x tools/refresh_data_sources.py"
    )


def test_install_sh_does_editable_install() -> None:
    """断言 install.sh 做的是 editable 安装，而非断言它怎么写。

    这是**变更检测器**，不是正确性判据：它拦的是「安装入口做错了事」
    （退回手抄依赖、不再 editable 装项目本身），不拦「写法被重构」。
    有意重构（加 --no-deps、摘 -q、变量化、合并行）都不会误伤。
    """
    content = _install_sh_text("HEAD")

    # 1) 必须存在 editable 安装（写法不限：参数顺序、变量名、行数都不管）
    assert _EDITABLE_INSTALL.search(content), (
        "install.sh 不再做 editable 安装（找不到 install ... -e \".[dev]\"）。\n"
        "这通常意味着安装会装不上项目本身，或依赖不再走 pyproject.toml。"
    )

    # 2) 必须基于 pyproject 安装项目本身，不能退回手抄依赖
    assert _HAND_COPIED_DEPS not in content, (
        "install.sh 退回手抄依赖清单 —— 干净机器装不上项目本身，"
        "且 pyproject.toml 里新增的依赖不会装上。"
    )


def test_wheel_contains_rules_json() -> None:
    """构建 wheel 时 rules.json 必须被打包进去。

    rules.json 是红线规则的数据源，漏了它等于装了包却没有规则。
    """
    dist = Path("dist")
    wheels = sorted(glob.glob(str(dist / "*.whl")))
    if not wheels:
        # 没有现成 wheel 就现场构建一个（CI 里 build 已装）
        try:
            subprocess.run(
                ["python", "-m", "build", "--wheel", "--outdir", str(dist), "."],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("无法构建 wheel（缺 build 模块），跳过打包内容校验")
        wheels = sorted(glob.glob(str(dist / "*.whl")))
    assert wheels, "没有构建出 wheel"

    with zipfile.ZipFile(wheels[0]) as whl:
        names = whl.namelist()
        assert any("rules.json" in n for n in names), (
            f"rules.json 没进 wheel：{names[:10]}...\n"
            "检查 pyproject.toml 的 [tool.setuptools.package-data]。"
        )


def test_install_sh_runs_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """install.sh 必须能在离线环境下执行到完成。

    用 `bash install.sh` 而不是 `./install.sh`：mode 由上面两条断言负责，
    本步骤负责判定脚本本身能跑通，两件事分开，本步骤就不依赖它正在保护的那个位。

    设 COMPANY_DD_SKIP_TESTS=1 跳过 install.sh 内部那次 pytest——
    本用例就在 tests/ 里，不跳过会无限递归。
    """
    env = {
        **os.environ,
        "COMPANY_DD_HOME": str(tmp_path / "data"),
        # 必须设：install.sh 自己会跑 pytest，不跳过就会无限递归
        # （本用例在 tests/ 里 → install.sh → pytest tests → 本用例 → ...）
        "COMPANY_DD_SKIP_TESTS": "1",
    }

    result = subprocess.run(
        ["bash", "install.sh"],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    assert result.returncode == 0, (
        f"install.sh 执行失败（exit {result.returncode}）:\n"
        f"--- stderr ---\n{result.stderr[-3000:]}\n"
        f"--- stdout ---\n{result.stdout[-3000:]}"
    )
