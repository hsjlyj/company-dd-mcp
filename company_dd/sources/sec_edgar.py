"""SEC EDGAR —— 美国上市公司官方披露（免 Key、无验证码、可复核）。

SEC 要求声明 User-Agent 并限速（<=10 req/s）；本工具按主机限速，默认更保守。
覆盖：主体身份（CIK、EIN、州、财年、曾用名、地址）、申报清单、XBRL 主要财务口径。
不覆盖：非美国主体、非上市公司、股东穿透。
"""

from __future__ import annotations

from typing import Any

from ..errors import DdError
from ..models import SourceStatus
from .base import Source, SourceSpec

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"

CONCEPTS = {
    "Revenues": "营业收入(Revenues)",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "营业收入(合同收入口径)",
    "Assets": "总资产",
    "Liabilities": "总负债",
    "NetIncomeLoss": "净利润",
    "StockholdersEquity": "股东权益",
}


class SecEdgarSource(Source):
    spec = SourceSpec(
        id="sec_edgar",
        label="SEC EDGAR（美国证监会官方披露）",
        kind="official",
        jurisdiction="US",
        homepage="https://www.sec.gov/edgar/search/",
        capabilities=["resolve", "entity_profile", "filings", "financials"],
        note="官方源，免 Key；需声明 UA 并限速。",
    )

    def probe(self) -> SourceStatus:  # type: ignore[override]
        try:
            data = self._json(SUBMISSIONS.format(cik=320193))
            return self.ok(SUBMISSIONS.format(cik=320193), f"读通 Apple Inc. (CIK 320193)，ticker={data.get('tickers')}")
        except Exception as exc:  # noqa: BLE001
            return self.failed(SUBMISSIONS.format(cik=320193), exc)

    def _json(self, url: str) -> dict[str, Any]:
        r = self.http.request(
            "GET", url, source=self.id, label=self.spec.label,
            headers={"Accept": "application/json", "Referer": "https://www.sec.gov/"},
        )
        return r.json()

    # ---------- 解析 ----------
    def resolve(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        data = self._json(TICKERS)
        rows = list(data.values()) if isinstance(data, dict) else []
        key = name.strip().lower()
        hits = []
        for row in rows:
            title = str(row.get("title") or "")
            ticker = str(row.get("ticker") or "")
            if title.lower() == key or ticker.lower() == key:
                hits.insert(0, row)
            elif key and (key in title.lower()):
                hits.append(row)
        out = []
        for row in hits[:limit]:
            out.append(
                {
                    "name": row.get("title"),
                    "ticker": row.get("ticker"),
                    "cik": int(row.get("cik_str") or 0),
                    "jurisdiction": "US",
                    "source_id": self.id,
                    "raw": row,
                }
            )
        return out

    def profile(self, cik: int) -> dict[str, Any]:
        data = self._json(SUBMISSIONS.format(cik=cik))
        addr = (data.get("addresses") or {}).get("business") or {}
        return {
            "cik": cik,
            "name": data.get("name"),
            "tickers": data.get("tickers") or [],
            "exchanges": data.get("exchanges") or [],
            "entity_type": data.get("entityType"),
            "sic": data.get("sic"),
            "sic_description": data.get("sicDescription"),
            "ein": data.get("ein"),
            "lei": data.get("lei"),
            "state_of_incorporation": data.get("stateOfIncorporation"),
            "fiscal_year_end": data.get("fiscalYearEnd"),
            "phone": data.get("phone"),
            "former_names": [
                f.get("name") for f in (data.get("formerNames") or []) if f.get("name")
            ],
            "address": ", ".join(
                str(addr.get(k)) for k in ("street1", "street2", "city", "stateOrCountry", "zipCode")
                if addr.get(k)
            ),
            "raw_meta": {"cik": data.get("cik"), "category": data.get("category")},
        }

    def filings(self, cik: int, form_types: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        data = self._json(SUBMISSIONS.format(cik=cik))
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        documents = recent.get("primaryDocument") or []
        filing_dates = recent.get("filingDate") or []
        report_dates = recent.get("reportDate") or []
        descriptions = recent.get("primaryDocDescription") or []

        def _at(rows: list[Any], index: int) -> Any:
            return rows[index] if index < len(rows) else ""

        items = []
        for i, form in enumerate(forms):
            if form_types and form not in form_types:
                continue
            accession = str(_at(accessions, i) or "")
            acc = accession.replace("-", "")
            doc = str(_at(documents, i) or "")
            items.append(
                {
                    "form": form,
                    "filing_date": _at(filing_dates, i),
                    "report_date": _at(report_dates, i),
                    "accession": accession,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}" if acc and doc else "",
                    "description": _at(descriptions, i),
                }
            )
            if len(items) >= limit:
                break
        return {"items": items, "raw_total": len(forms)}

    def financials(self, cik: int, tags: list[str] | None = None) -> dict[str, Any]:
        tags = tags or ["Revenues", "Assets", "Liabilities", "NetIncomeLoss"]
        out: dict[str, Any] = {}
        for tag in tags:
            label = CONCEPTS.get(tag, tag)
            try:
                data = self._json(CONCEPT.format(cik=cik, tag=tag))
            except DdError:
                out[tag] = {"label": label, "status": "unavailable"}
                continue
            units = data.get("units") or {}
            series = []
            for unit, rows in units.items():
                for row in rows:
                    if row.get("form") in ("10-K", "10-Q", "20-F", "40-F") and row.get("val") is not None:
                        series.append(
                            {
                                "end": row.get("end"),
                                "value": row.get("val"),
                                "unit": unit,
                                "form": row.get("form"),
                                "fy": row.get("fy"),
                                "fp": row.get("fp"),
                            }
                        )
            series.sort(key=lambda x: x.get("end") or "", reverse=True)
            out[tag] = {
                "label": label,
                "status": "ok" if series else "no_data",
                "latest": series[:4],
                "url": CONCEPT.format(cik=cik, tag=tag),
            }
        return out

    def cik_from_ticker(self, ticker: str) -> int | None:
        data = self._json(TICKERS)
        rows = list(data.values()) if isinstance(data, dict) else []
        for row in rows:
            if str(row.get("ticker", "")).upper() == ticker.upper():
                return int(row.get("cik_str") or 0)
        return None
