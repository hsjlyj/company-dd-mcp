"""结构化错误：每个失败都带 code + 人可读原因 + HTTP 状态 + 人工入口。

不写「请充值」「禁止改用其他工具」这类把商业意图塞进 Agent 上下文的话术。
"""

from __future__ import annotations

from typing import Any


class DdError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        http_status: int | None = None,
        url: str = "",
        source: str = "",
        hint: str = "",
        retryable: bool = False,
        manual_url: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.url = url
        self.source = source
        self.hint = hint
        self.retryable = retryable
        self.manual_url = manual_url

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "source": self.source,
        }
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.url:
            out["url"] = self.url
        if self.hint:
            out["hint"] = self.hint
        if self.retryable:
            out["retryable"] = True
        if self.manual_url:
            out["manual_url"] = self.manual_url
            out["next_step"] = "该渠道需人工在浏览器完成验证码/登录后核对，结果由用户确认，工具不代替判断。"
        return out


# WAF / 反爬常见状态码
BLOCKED_STATUS = {401, 403, 405, 412, 418, 429, 451, 502, 503, 521, 522, 524}

CAPTCHA_MARKERS = (
    "验证码",
    "请输入验证码",
    "captcha",
    "安全验证",
    "人机验证",
    "slider",
    "geetest",
    "access denied",
    "请求异常",
)


def is_blocked(status: int, body: str = "") -> bool:
    if status in BLOCKED_STATUS:
        return True
    low = (body or "").lower()
    return any(m in low for m in CAPTCHA_MARKERS)


def blocked_error(source: str, label: str, url: str, status: int, manual_url: str = "") -> DdError:
    return DdError(
        f"{label} 拒绝了本次请求（HTTP {status}）：该站点对非浏览器流量有 WAF/验证码拦截。",
        code="blocked",
        http_status=status,
        url=url,
        source=source,
        hint="这不是「查不到」，而是「查不了」。该维度已进入未验证清单，结论不能判绿。",
        manual_url=manual_url or url,
    )
