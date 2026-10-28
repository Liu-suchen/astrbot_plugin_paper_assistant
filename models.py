from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from urllib.parse import unquote, urlsplit

from .settings import settings

_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*(?:\.[A-Z]{2})?/\d{7})(?:v[1-9]\d*)?"


class PaperError(Exception):
    """A message that can be shown without leaking remote response bodies."""


def arxiv_id(value: str) -> str:
    value = unquote(value.strip().strip("<>"))
    if "://" in value:
        parts = urlsplit(value)
        if parts.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            return ""
        value = re.sub(r"^/(?:abs|pdf)/", "", parts.path).removesuffix(".pdf")
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    return value if re.fullmatch(_ID, value) else ""


def base_id(value: str) -> str:
    return re.sub(r"v[1-9]\d*$", "", value)


def title_key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


@dataclass
class Paper:
    title: str
    arxiv: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    published: str = ""
    doi: str = ""
    categories: list[str] = field(default_factory=list)
    zotero_key: str = ""
    zotero_url: str = ""

    @property
    def pdf_url(self) -> str:
        return settings().url("arxiv_web_base", f"pdf/{self.arxiv}") if self.arxiv else ""

    def dump(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, data: dict) -> Paper:
        return cls(**data)


def from_zotero(item: dict) -> Paper:
    data = item.get("data", item)
    identifier = arxiv_id(data.get("url", "")) or arxiv_id(data.get("archiveID", ""))
    if not identifier:
        match = re.search(r"arxiv:\s*(" + _ID + r")", data.get("extra", ""), re.I)
        identifier = arxiv_id(match[1]) if match else ""
    authors = [
        c.get("name") or " ".join(filter(None, [c.get("firstName"), c.get("lastName")]))
        for c in data.get("creators", [])
        if c.get("creatorType") == "author"
    ]
    return Paper(
        title=data.get("title", "无标题"),
        arxiv=identifier,
        authors=authors,
        abstract=data.get("abstractNote", ""),
        published=data.get("date", ""),
        doi=data.get("DOI", ""),
        zotero_key=item.get("key", data.get("key", "")),
        zotero_url=item.get("links", {}).get("alternate", {}).get("href", ""),
    )


def card(paper: Paper, config=None) -> str:
    config = settings(config)
    lines = [
        paper.title,
        f"作者：{', '.join(paper.authors[:6]) or '未提供'}",
        f"日期：{paper.published or '未提供'}",
    ]
    if paper.arxiv:
        lines.extend([f"arXiv：{paper.arxiv}", f'原文：{config.url("arxiv_web_base", "pdf/" + paper.arxiv)}'])
    if paper.zotero_key:
        lines.append(f"Zotero：{paper.zotero_url or paper.zotero_key}")
    return "\n".join(lines)


def chunks(text: str, size: int | None = None) -> list[str]:
    size = settings().get("message_chunk_chars") if size is None else size
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]
