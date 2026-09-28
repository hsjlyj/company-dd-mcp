"""Wikidata —— 主体身份消歧与关联关系（免 Key、CC0）。

用途不是「查工商」，而是：
  * 把一个名字收敛到确定的实体（QID），拿到 LEI / 官网 / 成立时间 / 总部 / 母公司；
  * 给跨辖区主体提供统一锚点（尤其非中美主体）；
  * 母公司/子公司关系线索，供人工继续穿透。
所有取值都来自维基数据声明，可能滞后，状态标 partial。
"""

from __future__ import annotations

from typing import Any

from .base import Source, SourceSpec

API = "https://www.wikidata.org/w/api.php"

PROPS = {
    "P1278": ("lei", "LEI"),
    "P17": ("country", "国家/地区"),
    "P571": ("inception", "成立时间"),
    "P127": ("parent", "母公司"),
    "P159": ("hq", "总部"),
    "P1128": ("employees", "员工数"),
    "P856": ("website", "官网"),
    "P414": ("exchange", "上市交易所"),
    "P249": ("ticker", "股票代码"),
    "P946": ("isin", "ISIN"),
    "P1454": ("legal_form", "法律形式"),
    "P112": ("founded_by", "创始人"),
    "P3320": ("board_member", "董事会成员"),
    "P169": ("ceo", "CEO"),
}


class WikidataSource(Source):
    spec = SourceSpec(
        id="wikidata",
        label="Wikidata（CC0 知识库）",
        kind="third_party",
        jurisdiction="GLOBAL",
        homepage="https://www.wikidata.org/",
        capabilities=["resolve", "identity", "relations"],
        note="身份消歧与关联关系；字段可能滞后，不作工商凭证。",
    )

    def _api(self, **params: Any) -> dict[str, Any]:
        params.setdefault("format", "json")
        r = self.http.request(
            "GET", API, source=self.id, label=self.spec.label, params=params,
            headers={"Accept": "application/json"},
        )
        return r.json()

    def probe(self) -> SourceStatus:  # type: ignore[override]
        try:
            data = self._api(action="wbsearchentities", search="Anker Innovations",
                             language="en", limit=1)
            hits = data.get("search") or []
            return self.ok(API, f"接口正常，样例命中 {len(hits)} 条")
        except Exception as exc:  # noqa: BLE001
            return self.failed(API, exc)

    def resolve(self, name: str, limit: int = 5, language: str = "en") -> list[dict[str, Any]]:
        data = self._api(action="wbsearchentities", search=name, language=language, limit=limit,
                         uselang=language)
        out = []
        for hit in data.get("search") or []:
            out.append(
                {
                    "qid": hit.get("id"),
                    "name": hit.get("label"),
                    "description": hit.get("description"),
                    "jurisdiction": "GLOBAL",
                    "source_id": self.id,
                    "raw": hit,
                }
            )
        return out

    def entity(self, qid: str) -> dict[str, Any]:
        data = self._api(action="wbgetentities", ids=qid, props="claims|labels|descriptions",
                         languages="en|zh")
        ent = (data.get("entities") or {}).get(qid) or {}
        claims = ent.get("claims") or {}
        facts: dict[str, Any] = {}
        refs: set[str] = set()

        def claim_values(pid: str) -> list[Any]:
            vals = []
            for c in claims.get(pid, []):
                mv = (((c.get("mainsnak") or {}).get("datavalue")) or {})
                v = mv.get("value")
                if isinstance(v, dict):
                    if "time" in v:
                        vals.append(v["time"].lstrip("+")[:10])
                    elif "amount" in v:
                        vals.append(str(v["amount"]).lstrip("+"))
                    elif "id" in v:
                        vals.append(v["id"])
                        refs.add(v["id"])
                    elif "text" in v:
                        vals.append(v["text"])
                    else:
                        vals.append(v)
                elif v is not None:
                    vals.append(v)
            return vals

        for pid, (key, label) in PROPS.items():
            vals = claim_values(pid)
            if vals:
                facts[key] = {"label": label, "values": vals}

        labels = self._label_map(sorted(refs))
        for key, item in facts.items():
            item["values"] = [labels.get(v, v) if isinstance(v, str) else v for v in item["values"]]

        return {
            "qid": qid,
            "label": ((ent.get("labels") or {}).get("en") or {}).get("value"),
            "label_zh": ((ent.get("labels") or {}).get("zh") or {}).get("value"),
            "description": ((ent.get("descriptions") or {}).get("en") or {}).get("value"),
            "facts": facts,
            "url": f"https://www.wikidata.org/wiki/{qid}",
            "raw": ent,
        }

    def _label_map(self, qids: list[str]) -> dict[str, str]:
        if not qids:
            return {}
        data = self._api(action="wbgetentities", ids="|".join(qids[:50]), props="labels",
                         languages="en|zh")
        out = {}
        for qid, ent in (data.get("entities") or {}).items():
            labs = ent.get("labels") or {}
            out[qid] = (labs.get("zh") or labs.get("en") or {}).get("value", qid)
        return out
