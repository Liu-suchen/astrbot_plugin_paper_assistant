"""Deployment settings; defaults are shared with AstrBot's configuration UI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))

BOUNDS = {
    "http_timeout": (5, 300),
    "http_get_attempts": (1, 5),
    "http_retry_max_delay": (1, 120),
    "job_timeout": (30, 3600),
    "worker_interval": (1, 60),
    "delivery_attempts": (1, 10),
    "max_pending_per_user": (1, 50),
    "candidate_limit": (1, 20),
    "choice_ttl": (60, 86400),
    "message_chunk_chars": (200, 4000),
    "max_pdf_mb": (1, 200),
    "max_pdf_chars": (1000, 200000),
    "max_summary_chars": (1000, 200000),
    "file_link_ttl": (60, 3600),
    "arxiv_min_interval": (3, 60),
}

ENDPOINTS = {
    "arxiv_api_url", "arxiv_web_base", "zotero_api_base",
    "zotero_web_base", "papers_cool_base", "translation_site_url",
}


def invalid(message):
    from .models import PaperError

    raise PaperError(message)


class Settings:
    def __init__(self, source=None):
        self.source = source if source is not None else {}

    def get(self, key, default=None):
        definition = SCHEMA.get(key, {})
        value = self.source.get(key, definition.get("default", default))
        if key in BOUNDS:
            low, high = BOUNDS[key]
            kind = float if definition.get("type") == "float" else int
            try:
                if isinstance(value, bool):
                    raise ValueError
                if kind is int and float(value) != int(value):
                    raise ValueError
                value = kind(value)
                if not low <= value <= high:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                invalid(f"插件配置 {key} 必须在 {low} 至 {high} 之间。")
        if definition.get("type") == "list" and not isinstance(value, list):
            invalid(f"插件配置 {key} 应为列表。")
        if definition.get("type") == "bool" and not isinstance(value, bool):
            invalid(f"插件配置 {key} 应为开关。")
        if key in ENDPOINTS:
            value = self._url(key, value, https_only=True)
        if key == "file_callback_base" and value:
            value = self._url(key, value, https_only=False)
        if definition.get("options") and value not in definition["options"]:
            invalid(f"插件配置 {key} 的选项不正确。")
        return value

    @staticmethod
    def _url(key, value, *, https_only):
        if not isinstance(value, str):
            invalid(f"插件配置 {key} 应为服务地址。")
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        schemes = {"https"} if https_only else {"https", "http"}
        if parts.scheme not in schemes or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            invalid(f"插件配置 {key} 需要有效的{'HTTPS' if https_only else 'HTTP(S)'}地址，不能包含凭据、查询或片段。")
        return value

    def validate(self):
        for key in SCHEMA:
            self.get(key)

    def url(self, key, path=""):
        return self.get(key).rstrip("/") + ("/" + path.lstrip("/") if path else "")

    def library_scope(self):
        identity = [self.get("zotero_api_base"), self.get("zotero_library_type"),
                    str(self.get("zotero_library_id", "")).strip()]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]

    def fingerprint(self, keys):
        values = {key: self.get(key) for key in keys}
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]


def settings(config=None):
    return config if isinstance(config, Settings) else Settings(config)


def file_transport(config, global_callback=""):
    config = settings(config)
    mode = config.get("file_transfer_mode")
    if mode == "local":
        return "local", ""
    base = config.get("file_callback_base") or global_callback
    if base:
        base = Settings._url("file_callback_base", base, https_only=False)
        return "callback", base
    if mode == "callback":
        invalid("请配置消息平台可访问的 file_callback_base，或 AstrBot 全局 callback_api_base。")
    return "local", ""
