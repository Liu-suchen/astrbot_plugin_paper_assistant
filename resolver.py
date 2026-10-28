from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from .models import Paper, PaperError, arxiv_id
from .net import Http
from .settings import settings


class Arxiv:
    def __init__(self, http: Http, config=None):
        self.config = settings(config)
        self.http = http
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def resolve(self, query: str) -> list[Paper]:
        identifier = arxiv_id(query)
        if not identifier and ("://" in query or query.lower().startswith("arxiv:")):
            raise PaperError("请输入有效的 arXiv 编号、arxiv.org 链接或论文标题。")
        if len(query) > 500:
            raise PaperError("论文标题过长，请改用 arXiv 编号。")
        if identifier:
            params = {"id_list": identifier}
        else:
            words = re.findall(r"[\w-]+", query, flags=re.UNICODE)
            if not words:
                raise PaperError("请输入论文标题。")
            params = {
                "search_query": " AND ".join(f'ti:"{w}"' for w in words[:24]),
                "max_results": str(self.config.get("candidate_limit")),
                "sortBy": "relevance",
            }
        async with self.lock:
            delay = max(0, self.config.get("arxiv_min_interval") - (time.monotonic() - self.last_request))
            await asyncio.sleep(delay)
            self.last_request = time.monotonic()
            response = await self.http.request(
                "GET", self.config.get("arxiv_api_url") + "?" + urlencode(params)
            )
        papers = self.parse(response.body)
        if identifier and papers and re.search(r"v\d+$", identifier):
            if papers[0].arxiv != identifier:
                raise PaperError("arXiv 未返回指定版本，未自动替换为其他版本。")
        return papers

    @staticmethod
    def parse(body: bytes) -> list[Paper]:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise PaperError("arXiv 返回了不支持的数据。")
        ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise PaperError("arXiv 数据解析失败。") from exc
        papers = []
        for entry in root.findall("a:entry", ns):
            identifier = arxiv_id(entry.findtext("a:id", "", ns))
            if not identifier:
                continue
            papers.append(Paper(
                title=" ".join(entry.findtext("a:title", "", ns).split()),
                arxiv=identifier,
                authors=[a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)],
                abstract=" ".join(entry.findtext("a:summary", "", ns).split()),
                published=entry.findtext("a:published", "", ns)[:10],
                doi=entry.findtext("x:doi", "", ns),
                categories=[c.get("term", "") for c in entry.findall("a:category", ns)],
            ))
        return papers
