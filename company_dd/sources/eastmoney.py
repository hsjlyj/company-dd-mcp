"""东方财富 F10 数据中心 —— 上市公司工商/股东/财务字段。

为什么用它：它是「聚合商」而不是官方源，所以工具把它标成 aggregator，
字段状态最多给到 partial，必须能和 cninfo 公告、官方渠道交叉验证后才敢标 verified。
它提供真实可复核的结构化字段：统一社会信用代码、法定代表人、注册资本、成立日期、
注册地址、经营范围、员工人数、董监高、十大股东、主要财务指标。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..errors import DdError
from ..models import SourceStatus
from .base import Source, SourceSpec

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REFERER = "https://emweb.securities.eastmoney.com/"
SURVEY = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax"

REPORTS = {
    "basic_orginfo": "RPT_F10_BASIC_ORGINFO",
    "top_holders": "RPT_F10_EH_HOLDERS",
    "free_holders": "RPT_F10_EH_FREEHOLDERS",
    "main_finance": "RPT_F10_FINANCE_MAINFINADATA",
}

# 只取背调关心的字段，避免把上百个字段倒给模型
FIELD_LABELS = {
    "ORG_NAME": "企业全称",
    "ORG_NAME_EN": "英文名称",
    "REG_NUM": "统一社会信用代码",
    "LEGAL_PERSON": "法定代表人",
    "REG_CAPITAL": "注册资本（万元）",
    "FOUND_DATE": "成立日期",
    "LISTING_DATE": "上市日期",
    "REG_ADDRESS": "注册地址",
    "ADDRESS": "办公地址",
    "BUSINESS_SCOPE": "经营范围",
    "MAIN_BUSINESS": "主营业务",
    "EMP_NUM": "员工人数",
    "PROVINCE": "注册省份",
    "EM2016": "行业（东财分类）",
    "INDUSTRYCSRC1": "行业（证监会分类）",
    "CHAIRMAN": "董事长",
    "PRESIDENT": "总经理",
    "SECRETARY": "董事会秘书",
    "SECPRESENT": "证券事务代表",
    "ACTUAL_HOLDER": "实际控制人",
    "INDEDIRECTORS": "独立董事",
    "ORG_TEL": "联系电话",
    "ORG_EMAIL": "电子邮箱",
    "ORG_WEB": "公司网址",
    "SECURITY_TYPE": "证券类型",
    "TRADE_MARKET": "交易市场",
    "ACCOUNTFIRM_NAME": "会计师事务所",
    "LAW_FIRM": "律师事务所",
    "TATOLNUMBER": "子公司数量",
}


class EastmoneySource(Source):
    spec = SourceSpec(
        id="eastmoney",
        label="东方财富 F10 数据中心（第三方聚合）",
        kind="aggregator",
        jurisdiction="CN",
        homepage="https://emweb.securities.eastmoney.com/",
        capabilities=["listed_profile", "shareholders", "financials"],
        note="聚合商，非官方源；字段默认 partial，需与公告交叉验证。",
    )

    def probe(self) -> SourceStatus:  # type: ignore[override]
        try:
            data = self._get(REPORTS["basic_orginfo"], '(SECUCODE="300750.SZ")', page_size=1)
            rows = data.get("result", {}).get("data") or []
            return self.ok(API, f"接口正常，样例字段 {len(rows[0]) if rows else 0} 个")
        except Exception as exc:  # noqa: BLE001
            return self.failed(API, exc)

    def _get(self, report: str, filt: str, page_size: int = 1, sort: str = "") -> dict[str, Any]:
        params = {
            "reportName": report,
            "columns": "ALL",
            "filter": filt,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "source": "WEB",
            "client": "WEB",
        }
        if sort:
            params["sortColumns"] = sort
            params["sortTypes"] = "-1"
        r = self.http.request(
            "GET", API, source=self.id, label=self.spec.label,
            params=params, headers={"Referer": REFERER},
        )
        data = r.json()
        if data.get("success") is False:
            raise DdError(
                f"报表不可用：{data.get('message')}",
                code="unsupported_report",
                url=API,
                source=self.id,
            )
        return data

    def basic_orginfo(self, secucode: str) -> dict[str, Any]:
        data = self._get(REPORTS["basic_orginfo"], f'(SECUCODE="{secucode}")')
        rows = data.get("result", {}).get("data") or []
        if not rows:
            return {"found": False, "secucode": secucode, "raw": data}
        row = rows[0]
        picked = {k: row.get(k) for k in FIELD_LABELS if row.get(k) not in (None, "", "--")}
        return {"found": True, "secucode": secucode, "fields": picked, "raw": row}

    def shareholders(self, secucode: str, limit: int = 10) -> dict[str, Any]:
        data = self._get(REPORTS["top_holders"], f'(SECUCODE="{secucode}")', page_size=limit)
        rows = data.get("result", {}).get("data") or []
        items = [
            {
                "rank": r.get("HOLDER_RANK"),
                "name": r.get("HOLDER_NAME"),
                "hold_ratio": r.get("HOLD_NUM_RATIO"),
                "hold_num": r.get("HOLD_NUM"),
                "end_date": (r.get("END_DATE") or "")[:10],
            }
            for r in rows
        ]
        return {"items": items, "raw": rows}

    def main_finance(self, secucode: str, limit: int = 4) -> dict[str, Any]:
        data = self._get(REPORTS["main_finance"], f'(SECUCODE="{secucode}")', page_size=limit)
        rows = data.get("result", {}).get("data") or []
        keys = [
            "REPORT_DATE", "TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT", "TOTAL_ASSETS",
            "TOTAL_LIABILITIES", "DEBT_ASSET_RATIO", "ROE_WEIGHT", "GROSS_PROFIT_RATIO",
            "NETPROFIT_YOY", "TOTALOPERATEREVETZ",
        ]
        items = [{k: r.get(k) for k in keys if r.get(k) is not None} for r in rows]
        return {"items": items, "raw": rows}

    def survey(self, code_with_market: str) -> dict[str, Any]:
        """旧版 CompanySurveyAjax：code 形如 SZ300750 / SH600000。"""
        r = self.http.request(
            "GET", SURVEY, source=self.id, label=self.spec.label,
            params={"code": code_with_market}, headers={"Referer": REFERER},
        )
        return r.json()

    @staticmethod
    def quote(s: str) -> str:
        return urllib.parse.quote(s)
