"""中国官方渠道可达性探测 —— 只报真实状态，不假装能查。

GSXT / 信用中国 / 执行信息公开网 / 裁判文书网对这些出口流量返回 WAF 状态
(403/412/521) 或验证码。本模块把这一事实变成一等公民：
  status=blocked + 真实 HTTP 码 + 人工入口链接，
并把它写进报告的「未验证清单」，确保结论不会被误判成「没问题」。
"""

from __future__ import annotations

from typing import Any

from ..config import OFFICIAL_CN_SITES
from .base import Source, SourceSpec

MANUAL_LINKS = {
    "gsxt": "https://www.gsxt.gov.cn/index.html",
    "credit_china": "https://www.creditchina.gov.cn/xinyongfuwu/",
    "zxgk": "https://zxgk.court.gov.cn/",
    "wenshu": "https://wenshu.court.gov.cn/",
    "court_announce": "https://rmfygg.court.gov.cn/",
}

SEARCH_HINT = {
    "gsxt": "在首页搜索框输入企业全称，过滑块后核对「基础信息 / 经营异常 / 行政处罚 / 年报」。",
    "credit_china": "在「信用服务」里按企业名称查「行政处罚 / 失信记录 / 红黑名单」。",
    "zxgk": "分别在「被执行人 / 失信被执行人 / 限制消费」三个入口按企业名称检索。",
    "wenshu": "登录后按当事人名称检索裁判文书，注意同名主体，按案号核对。",
    "court_announce": "按当事人名称检索开庭/送达公告。",
}


class OfficialCnSource(Source):
    spec = SourceSpec(
        id="official_cn",
        label="中国官方渠道可达性（GSXT/信用中国/执行公开网/裁判文书网）",
        kind="official",
        jurisdiction="CN",
        homepage="https://www.gsxt.gov.cn/",
        capabilities=["probe", "manual_route"],
        note="这些站点对自动化流量有 WAF/验证码，工具只报可用性并给出人工入口。",
    )

    def probe_site(self, site: dict[str, Any]) -> Any:
        from ..models import SourceStatus, now_iso

        sid = site["id"]
        url = site["url"]
        manual = MANUAL_LINKS.get(sid, url)
        try:
            r = self.http.request("GET", url, source=sid, label=site["label"], manual_url=manual)
            return SourceStatus(
                source_id=sid,
                label=site["label"],
                kind="official",
                jurisdiction="CN",
                status="blocked",
                http_status=r.status,
                url=url,
                detail=(
                    f"首页可达（HTTP {r.status}），但检索入口需要登录/验证码，"
                    "本工具不代做验证码验证：该维度转人工清单。"
                ),
                fetched_at=now_iso(),
                manual_url=manual,
            )
        except Exception as exc:  # noqa: BLE001
            st = self.failed(manual, exc)
            st.source_id = sid
            st.label = site["label"]
            st.status = "blocked"
            st.detail = (
                f"{st.detail} 人工入口：{manual} {SEARCH_HINT.get(sid, '')}".strip()
            )
            st.manual_url = manual
            return st

    def probe(self):  # type: ignore[override]
        statuses = [self.probe_site(site) for site in OFFICIAL_CN_SITES]
        blocked = [s for s in statuses if s.status != "ok"]
        from ..models import SourceStatus, now_iso

        return SourceStatus(
            source_id=self.id,
            label=self.spec.label,
            kind=self.spec.kind,
            jurisdiction="CN",
            status="blocked" if blocked else "ok",
            url=self.spec.homepage,
            detail=f"共 {len(statuses)} 个官方站点，{len(blocked)} 个对自动化流量拦截；"
                   f"拦截项已转人工清单。",
            fetched_at=now_iso(),
            manual_url="https://www.gsxt.gov.cn/index.html",
        )

    def probe_all(self) -> list[Any]:
        return [self.probe_site(site) for site in OFFICIAL_CN_SITES]

    @staticmethod
    def manual_plan(subject_name: str) -> list[str]:
        name = subject_name or "<企业全称>"
        steps = []
        for sid, url in MANUAL_LINKS.items():
            steps.append(f"[{sid}] {url} — {SEARCH_HINT.get(sid, '')}（检索词：{name}）")
        steps.append(
            "[消歧] 招聘/合同/官网出现的名称可能是简称或另一注册主体；"
            "用统一社会信用代码作为唯一键核对，不要按名字判定。"
        )
        steps.append(
            "[留痕] 人工核查结果请截图或导出 PDF 存入证据目录，报告中标注核查时间与操作人。"
        )
        return steps
