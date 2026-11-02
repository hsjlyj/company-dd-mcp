"""可靠性与安全回归：默认配置可运行、证据哈希可复核、报告 ID 不能穿越路径。"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
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
#     （100644），`./install.sh` 实测 exit 126。此前没有任何 CI 断言在看它。
#
#     而且它被**修过两次，两次都没修成**（试金 逐提交核过 blob 与 mode）：
#
#       7fed6cc  声称 chore: restore executable bit on install.sh
#                → mode 前后都是 100644（没修成）
#                → 同时把 install.sh 从 ef5e31f 换成了 34e769f（31 行 → 50 行）
#                  即：声明的没做成，没声明的做了一件
#
#       f475c8e  声称 chore: restore executable bit on tools/refresh_data_sources.py
#                → 该文件 blob 与 mode 前后逐字节相同（6791cae / 100644）
#                  即：一笔空操作，什么都没做
#
#       c83b092a test: add regression coverage
#                → install.sh 又被从 34e769f 换回 ef5e31f，mode 依然没动
#
#     注意这两笔的失效模式**不同**：一个「改错了」（顺手换了文件内容），
#     一个「以为改了但没改」（空操作）。别把它们压成「掉了两次」——
#     混为一谈会让人得出「语义断言没用」的错误结论，而正确结论是
#     它们需要**不同的**防线：内容断言 + mode 断言各拦一类。
#
#     两笔当时都没有任何东西发现它们没生效。这两条 mode 断言如果当时
#     就在 CI 里，上面两笔会当场变红 —— 这就是它们存在的全部理由。
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
#
# 这道正则的天花板（试金 实测，写下来免得下一个人以为它更强）：
#   * 加行首锚定后拦得住「把真行注释掉」（含缩进后注释）。
#   * 拦不住「真行还在但不再执行」：改成 echo '...'、包进 if false; then。
#     那两行里 install 后面照样跟着 -e ".[dev]"，正则只看得见文本。
#   * 真正覆盖「这行会被执行」的是 test_install_sh_runs_smoke：它跑 install.sh
#     并验证项目真的被装上。防篡改交给 mode 断言 + blob 评审清单，
#     本正则只负责「快速失败 + 给出可读原因」，不承担防篡改职责。
#
# 坐标写法提醒：7fed6cc 的**父提交**是 c83b092a，只是它当时持有的
# install.sh 是 ef5e31f。别写成「7fed6cc 的父提交是 ef5e31f」——
# 那是提交坐标，下一个人拿去 git show 会找不到（f436cd9 那个坑的翻版）。
# ---------------------------------------------------------------------------

# editable 安装：行首锚定并排除注释行；install 与 -e ".[dev]" 之间允许有任何参数。
_EDITABLE_INSTALL = re.compile(r'^[^#\n]*\binstall\b[^\n]*\s-e\s+"?\.\[dev\]"?', re.MULTILINE)

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

    每次先清掉旧产物再构建：复用现成 wheel 会在脏工作区里假绿。
    实测（试金 反例，我复现）：把 pyproject.toml 的
    [tool.setuptools.package-data] 清成 company_dd = [] 之后 ——

        rm -rf dist build *.egg-info  →  FAILED  ✅ 正确报红
        rm -rf dist *.egg-info        →  passed  ❌ 假绿

    后者假绿的原因：dist/*.whl 是上一次构建的产物，而
    build/lib/company_dd/rules.json 这个残留会把 rules.json 重新塞进
    下一次产出的 wheel。断言读的是「可能过期的产物」，不是「当前配置的
    真实结果」。CI 上是干净 clone 所以不受影响，但本地
    `./install.sh`（它会跑 pytest 并产出 dist/）之后再单跑测试就是脏场景。
    代价是每次真构建一次 wheel（约 2~5 秒），换来的是不再假绿。
    """
    dist = Path("dist")
    # 必须无条件构建，且两条旧产物都要清掉。
    #
    # 试金 实测的三条对照（我逐条复现，结果一字不差）。攻击方式都是把
    # pyproject.toml 的 [tool.setuptools.package-data] 清成 company_dd = []：
    #
    #   A 什么都不删（dist 有 + build 有） → PASS ❌ 0.06s，根本没构建
    #   B 只删 dist，保留 build          → PASS ❌ 2.04s，构建了但仍带 rules.json
    #   C 两个都删                       → FAIL ✅ 2.02s，正确报红
    #
    # 两条独立的假绿路径：
    #   1) 旧 dist/*.whl 被 glob 直接命中，断言读的是上一次构建的产物；
    #   2) build/lib/company_dd/rules.json 残留会在新构建时把 rules.json
    #      重新打进 wheel，即使 package-data 已经坏了。
    # 所以只清 dist 挡不住 B，只清 build 挡不住 A，必须两条都清、
    # 并且去掉 `if not wheels` 这个复用分支 —— 它就是第 1 条短路的来源。
    #
    # 为什么值得付这个代价（每次真构建一次 wheel，约 2~5 秒）：
    # CI 是干净 clone，走的是对照 C，所以 CI 上这条断言本来就对；
    # 风险只在本地脏工作区 —— 而 `./install.sh` 第 30 行会跑 pytest
    # 并留下 dist/，所以「先 install 再单跑测试」正是最自然的本地路径，
    # 下一个改 pyproject.toml 的人会看到绿。这是假绿，不是真通过。
    shutil.rmtree(dist, ignore_errors=True)
    shutil.rmtree(Path("build"), ignore_errors=True)
    for stale in Path(".").glob("*.egg-info"):
        shutil.rmtree(stale, ignore_errors=True)

    # 用 sys.executable 而不是裸 "python"：裸 python 在 venv 里、
    # 在 CI 的 ubuntu runner 上都可能不存在（本机实测 `python: command
    # not found`），而它一旦不存在就会抛 FileNotFoundError，
    # 被 skip 兜底吞掉 —— 断言看起来在跑，实际一次都没执行。
    #
    # build 由 pyproject.toml 的 dev extra 提供（pytest + build）。
    # 如果它缺失，说明 dev extra 退化了 —— 那是产品缺陷（本地也构不出
    # wheel），必须报错而不是 skip。
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), "."],
        capture_output=True,
        text=True,
        check=True,
    )
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
