"""巨潮资讯网（cninfo）—— 深交所指定信息披露平台。

为什么选它：官方指定披露渠道，有公开 JSON 接口、无验证码、可给到公告原文 PDF 链接，
每条结果都能被人复核。覆盖 A 股 / 港股（H 股）上市公司公告与全文检索。

局限：只覆盖上市公司。非上市主体它没有，工具必须如实说「没有」，不能糊。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import DdError
from ..http import Response
from .base import Source, SourceSpec

TOPSearch = "https://www.cninfo.com.cn/new/information/topSearch/query"
ANNOUNCE = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
FULLTEXT = "https://www.cninfo.com.cn/new/fulltextSearch/full"
STOCK_LIST = "https://www.cninfo.com.cn/new/data/szse_stock.json"
STATIC = "https://static.cninfo.com.cn/"

COLUMN_BY_CATEGORY = {
    "A股": "szse",     # 接口接受 szse 覆盖深沪，sse 亦可
    "港股": "hke",
    "基金": "fund",
    "债券": "bond",
    "退市": "szse",
}


class CninfoSource(Source):
    spec = SourceSpec(
        id="cninfo",
        label="巨潮资讯网（深交所指定披露平台）",
        kind="official",
        jurisdiction="CN",
        homepage="https://www.cninfo.com.cn/",
        capabilities=["resolve", "announcements", "fulltext_search", "listed_profile"],
        note="A股/港股上市公司官方公告渠道；非上市主体无数据。",
    )

    # ---------- probe ----------
    def probe(self) -> SourceStatus:  # type: ignore[override]
        try:
            r = self.http.request(
                "POST", TOPSearch, source=self.id, label=self.spec.label,
                data={"keyWord": "平安银行", "maxNum": "1"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            items = r.json()
            return self.ok(TOPSearch, f"接口正常，样例命中 {len(items)} 条", r.status)
        except Exception as exc:  # noqa: BLE001
            return self.failed(TOPSearch, exc)

    # ---------- 名称解析 ----------
    def resolve(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        r: Response = self.http.request(
            "POST", TOPSearch, source=self.id, label=self.spec.label,
            data={"keyWord": name, "maxNum": str(limit)},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        rows = r.json()
        out: list[dict[str, Any]] = []
        for row in rows or []:
            out.append(
                {
                    "name": row.get("zwjc"),
                    "code": row.get("code"),
                    "category": row.get("category"),
                    "org_id": row.get("orgId"),
                    "pinyin": row.get("pinyin"),
                    "delisted": row.get("delisted") == "true",
                    "source_id": self.id,
                    "raw": {"topSearch": row},
                }
            )
        return out

    # ---------- 公告 ----------
    def announcements(
        self,
        *,
        code: str,
        org_id: str,
        column: str = "szse",
        search_key: str = "",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        payload = {
            "pageNum": str(page),
            "pageSize": str(page_size),
            "column": column,
            "tabName": "fulltext",
            "stock": f"{code},{org_id}",
            "searchkey": search_key,
            "seDate": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        r = self.http.request(
            "POST", ANNOUNCE, source=self.id, label=self.spec.label, data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = r.json()
        items = []
        for row in data.get("announcements") or []:
            items.append(self._norm_announcement(row))
        return {
            "total": data.get("totalRecordNum") or 0,
            "page": page,
            "page_size": page_size,
            "items": items,
            "raw": data,
        }

    def fulltext_search(self, keyword: str, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        params = {
            "searchkey": keyword, "sdate": "", "edate": "", "isfulltext": "false",
            "sortName": "nothing", "sortType": "desc", "pageNum": str(page),
            "pageSize": str(page_size),
        }
        r = self.http.request(
            "POST", FULLTEXT, source=self.id, label=self.spec.label,
            params=params, data={},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = r.json()
        return {
            "total": data.get("totalRecordNum") or 0,
            "items": [self._norm_announcement(x) for x in (data.get("announcements") or [])],
            "raw": data,
        }

    def stock_map(self) -> dict[str, dict[str, Any]]:
        r = self.http.request("GET", STOCK_LIST, source=self.id, label=self.spec.label)
        data = r.json()
        out: dict[str, dict[str, Any]] = {}
        for row in data.get("stockList") or []:
            out[str(row.get("code"))] = {"name": row.get("zwjc"), "org_id": row.get("orgId")}
        return out

    @staticmethod
    def _norm_announcement(row: dict[str, Any]) -> dict[str, Any]:
        adjunct = row.get("adjunctUrl") or ""
        ts = row.get("announcementTime")
        date = ""
        if isinstance(ts, (int, float)) and ts > 0:
            date = datetime.fromtimestamp(
                ts / 1000, tz=timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d")
        return {
            "title": (row.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""),
            "code": row.get("secCode"),
            "name": row.get("secName"),
            "announcement_id": row.get("announcementId"),
            "time_ms": ts,
            "date": date,
            "url": (STATIC + adjunct) if adjunct else "",
            "type": row.get("adjunctType"),
        }

    @staticmethod
    def secucode(code: str, category: str) -> str:
        """把 6 位代码 + 板块映射成东财的 SECUCODE。

        实测（2026-09-29）：
          600/601/603/605/688 → .SH｜000/001/002/003/300/301 → .SZ
          43/83/87 段（新三板/北交所老代码）→ **.NQ**（不是 .BJ，.BJ 查出来是空）
          920 段（北交所新代码）→ .BJ
          港股 → .HK｜基金 → .OF
        """
        code = str(code)
        if category == "港股":
            return f"{code}.HK"
        if category == "基金":
            return f"{code}.OF"
        if code.startswith(("600", "601", "603", "605", "688", "689")):
            return f"{code}.SH"
        if code.startswith(("000", "001", "002", "003", "300", "301")):
            return f"{code}.SZ"
        if code.startswith("920"):
            return f"{code}.BJ"
        if code.startswith(("43", "83", "87", "88")):
            return f"{code}.NQ"
        return f"{code}.SZ"

    def require_key(self) -> None:  # pragma: no cover - 兼容接口
        raise DdError("cninfo 无需密钥", code="not_applicable")
