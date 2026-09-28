"""HTTP 层：统一 UA、超时、有界重试、按主机限速、如实上报拦截。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config
from .errors import DdError, is_blocked

# stdio MCP 下 stdout 是协议通道：把第三方库的 INFO 日志压到 WARNING，避免污染。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("anyio").setLevel(logging.WARNING)


@dataclass
class Response:
    status: int
    url: str
    text: str
    headers: dict[str, str]

    def json(self) -> Any:
        try:
            return _json_loads(self.text)
        except Exception as exc:  # noqa: BLE001
            raise DdError(
                f"响应不是合法 JSON：{exc}",
                code="parse_error",
                http_status=self.status,
                url=self.url,
            ) from exc


def _json_loads(text: str) -> Any:
    import json

    return json.loads(text)


class HttpClient:
    """按主机限速的同步客户端。httpx 默认 trust_env=True，会走 HTTPS_PROXY。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._last_hit: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = self.config.min_host_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.time()

    def request(
        self,
        method: str,
        url: str,
        *,
        source: str = "",
        label: str = "",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        manual_url: str = "",
        allow_status: tuple[int, ...] = (200,),
    ) -> Response:
        head = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        head.update(headers or {})
        host = httpx.URL(url).host or ""
        last_err: DdError | None = None

        for attempt in range(self.config.max_retries + 1):
            self._throttle(host)
            try:
                with httpx.Client(timeout=self.config.timeout, follow_redirects=True) as client:
                    resp = client.request(
                        method, url, headers=head, params=params, data=data, json=json_body
                    )
            except httpx.HTTPError as exc:
                last_err = DdError(
                    f"网络错误：{exc}",
                    code="unavailable",
                    url=url,
                    source=source,
                    retryable=True,
                )
                if attempt < self.config.max_retries:
                    time.sleep(0.6 * (2**attempt))
                    continue
                raise last_err from exc

            body = resp.text[:8_000_000]
            if resp.status_code in allow_status:
                return Response(
                    status=resp.status_code,
                    url=str(resp.url),
                    text=body,
                    headers=dict(resp.headers),
                )

            if is_blocked(resp.status_code, body):
                from .errors import blocked_error

                raise blocked_error(source, label or host, str(resp.url), resp.status_code, manual_url)

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.config.max_retries:
                time.sleep(0.6 * (2**attempt))
                continue

            raise DdError(
                f"上游返回 HTTP {resp.status_code}",
                code="upstream_error",
                http_status=resp.status_code,
                url=str(resp.url),
                source=source,
            )

        raise last_err or DdError("请求失败", code="unavailable", url=url, source=source)
