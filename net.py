from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from .models import PaperError
from .settings import settings


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PaperError("远程服务返回的数据格式不正确。") from exc


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise PaperError("远程服务尝试跳转到非 HTTPS 地址。")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            for key in list(redirected.headers):
                if key.lower() in {"zotero-api-key", "authorization"}:
                    redirected.remove_header(key)
        return redirected


class Http:
    def __init__(self, timeout: int | None = None, config=None):
        self.config = settings(config)
        self.timeout = timeout if timeout is not None else self.config.get("http_timeout")
        self.opener = urllib.request.build_opener(SafeRedirect())

    async def request(
        self, method: str, url: str, *, headers=None, data=None, json_data=None,
        form=None, limit: int = 4 * 1024 * 1024,
    ) -> Response:
        headers = {"User-Agent": "AstrBot-PaperAssistant", **(headers or {})}
        if json_data is not None:
            data = json.dumps(json_data).encode()
            headers["Content-Type"] = "application/json"
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        def run():
            # Only idempotent reads retry automatically. Writes use stable item keys.
            attempts = self.config.get("http_get_attempts") if method == "GET" else 1
            for attempt in range(attempts):
                try:
                    req = urllib.request.Request(url, data=data, headers=headers, method=method)
                    with self.opener.open(req, timeout=self.timeout) as stream:
                        body = stream.read(limit + 1)
                        if len(body) > limit:
                            raise PaperError("远程文件超过大小限制。")
                        return Response(stream.status, body, dict(stream.headers))
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    retry_after = exc.headers.get("Retry-After", "1")
                    exc.close()
                    if method == "GET" and status in {429, 500, 502, 503, 504} and attempt < attempts - 1:
                        try:
                            delay = min(self.config.get("http_retry_max_delay"), max(1, float(retry_after)))
                        except ValueError:
                            delay = 2
                        time.sleep(delay)
                        continue
                    messages = {
                        401: "授权已失效，请检查插件凭据。",
                        403: "没有访问权限，请检查凭据与文献库权限。",
                        404: "未找到对应内容。",
                        412: "远程内容发生变化，请重新查询后重试。",
                        413: "存储空间不足或文件超过服务限制。",
                        429: "服务访问频率受限，请稍后重试。",
                    }
                    raise PaperError(messages.get(status, f"远程服务请求失败（HTTP {status}）。")) from None
                except (urllib.error.URLError, TimeoutError, OSError):
                    if method == "GET" and attempt < attempts - 1:
                        time.sleep(attempt + 1)
                        continue
                    raise PaperError("网络连接失败或超时，请稍后重试。") from None
            raise PaperError("网络请求失败。")

        if urlsplit(url).scheme != "https":
            raise PaperError("仅支持 HTTPS 服务。")
        return await asyncio.to_thread(run)
