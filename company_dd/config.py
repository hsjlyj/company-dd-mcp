"""运行配置：全部来自环境变量，密钥永不落盘、永不进日志。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__


def _env_path(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default

DEFAULT_UA = (
    "company-dd-mcp/{ver} (+https://localhost/company-dd; due-diligence research tool; "
    "respects robots and rate limits)"
)

# 需要密钥的数据源 → 环境变量名。缺 Key 时该源状态为 needs_key，不报错、不跳过记录。
KEY_ENV = {
    "opencorporates": "OPENCORPORATES_API_KEY",
    "devnors": "DEVNORS_API_KEY",
    "tianyancha_cli": "TYC_TOKEN",  # 天眼查官方 CLI（tyc login 后写入 ~/.tyc/config.json）
}

# 已知会被 WAF / 验证码拦住的中国官方站点：不假装能抓，直接如实标记。
OFFICIAL_CN_SITES = [
    {
        "id": "gsxt",
        "label": "国家企业信用信息公示系统",
        "url": "https://www.gsxt.gov.cn/",
        "covers": ["工商登记", "经营异常", "行政处罚", "年报"],
    },
    {
        "id": "credit_china",
        "label": "信用中国",
        "url": "https://www.creditchina.gov.cn/",
        "covers": ["行政处罚", "失信记录", "红黑名单"],
    },
    {
        "id": "zxgk",
        "label": "中国执行信息公开网",
        "url": "https://zxgk.court.gov.cn/",
        "covers": ["被执行人", "失信被执行人", "限制消费"],
    },
    {
        "id": "wenshu",
        "label": "中国裁判文书网",
        "url": "https://wenshu.court.gov.cn/",
        "covers": ["裁判文书", "案由", "当事人"],
    },
    {
        "id": "court_announce",
        "label": "人民法院公告网",
        "url": "https://rmfygg.court.gov.cn/",
        "covers": ["开庭公告", "送达公告"],
    },
]


@dataclass
class Config:
    home: Path = field(
        default_factory=lambda: _env_path("COMPANY_DD_HOME", Path.home() / ".company-dd")
    )
    timeout: float = field(default_factory=lambda: _env_float("COMPANY_DD_TIMEOUT", 20.0))
    max_retries: int = field(default_factory=lambda: _env_int("COMPANY_DD_RETRIES", 2))
    min_host_interval: float = field(
        default_factory=lambda: _env_float("COMPANY_DD_HOST_INTERVAL", 0.8)
    )
    user_agent: str = field(default_factory=lambda: os.getenv("COMPANY_DD_UA", "").strip())
    keys: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.user_agent:
            self.user_agent = DEFAULT_UA.format(ver=__version__)
        self.keys = {src: os.getenv(env, "").strip() for src, env in KEY_ENV.items()}
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "reports").mkdir(parents=True, exist_ok=True)

    def has_key(self, source_id: str) -> bool:
        return bool(self.keys.get(source_id))
