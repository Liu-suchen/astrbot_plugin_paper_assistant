from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from .models import Paper, PaperError, arxiv_id, base_id, from_zotero, title_key
from .net import Http
from .store import Store
from .settings import settings

KEY_PATTERN = re.compile(r"[A-Z0-9]{8}")


class Zotero:
    def __init__(self, http: Http, config, store: Store):
        self.http, self.config, self.store = http, settings(config), store

    def settings(self) -> tuple[str, dict]:
        library = str(self.config.get("zotero_library_id")).strip()
        token = str(self.config.get("zotero_api_key")).strip()
        kind = self.config.get("zotero_library_type")
        if not library.isdigit() or not token or kind not in {"users", "groups"}:
            raise PaperError("请在论文助手插件设置中填写 Zotero 文献库 ID、类型和专用 API Key。")
        return self.config.url("zotero_api_base", f"{kind}/{library}"), {
            "Zotero-API-Key": token, "Zotero-API-Version": "3",
        }

    async def api(self, method: str, path: str, **kwargs):
        root, headers = self.settings()
        headers.update(kwargs.pop("headers", {}))
        return await self.http.request(method, root + path, headers=headers, **kwargs)

    async def search(self, query: str) -> list[Paper]:
        identifier = arxiv_id(query)
        params = {"q": base_id(identifier) if identifier else query, "qmode": "everything", "limit": 100}
        response = await self.api("GET", "/items/top?" + urlencode(params))
        items = response.json()
        if not isinstance(items, list):
            raise PaperError("Zotero 检索结果格式不正确。")
        papers = [self.paper(i) for i in items if i.get("data", {}).get("itemType") not in {"note", "attachment", "annotation"}]
        if identifier:
            papers = [p for p in papers if base_id(p.arxiv) == base_id(identifier)]
        else:
            exact = title_key(query)
            papers.sort(key=lambda p: title_key(p.title) != exact)
        if len(papers) > self.config.get("candidate_limit"):
            raise PaperError(f'Zotero 匹配超过 {self.config.get("candidate_limit")} 篇，请提供更完整的标题或 arXiv 编号。')
        return papers

    def paper(self, item: dict) -> Paper:
        paper = from_zotero(item)
        if not paper.zotero_url:
            kind = self.config.get("zotero_library_type")
            library = self.config.get("zotero_library_id")
            paper.zotero_url = self.config.url("zotero_web_base", f"{kind}/{library}/items/{paper.zotero_key}")
        return paper

    async def children(self, key: str) -> list[dict]:
        if not KEY_PATTERN.fullmatch(key):
            raise PaperError("Zotero 条目编号不正确。")
        response = await self.api("GET", f"/items/{key}/children?limit=100")
        return response.json()

    async def get_item(self, key: str) -> dict:
        if not KEY_PATTERN.fullmatch(key):
            raise PaperError("Zotero 条目编号不正确。")
        return (await self.api("GET", f"/items/{key}")).json()

    async def create(self, data: dict) -> dict:
        response = (await self.api("POST", "/items", json_data=[data])).json()
        if response.get("failed"):
            raise PaperError("Zotero 拒绝写入，请检查集合编号、字段和写入权限。")
        successful = response.get("successful", {}).get("0")
        if successful:
            return successful
        key = response.get("success", {}).get("0") or response.get("unchanged", {}).get("0")
        if key:
            return await self.get_item(key)
        raise PaperError("Zotero 未确认写入成功，请查询后重试。")

    async def ensure_paper(self, paper: Paper) -> tuple[Paper, bool]:
        # A deterministic key makes retries after a lost POST response idempotent.
        root, _ = self.settings()
        identity = base_id(paper.arxiv) or paper.doi
        if not identity:
            raise PaperError("无法确定论文身份，未写入 Zotero。")
        existing = await self.search(paper.arxiv) if paper.arxiv else []
        if not existing and paper.doi:
            response = await self.api("GET", "/items/top?" + urlencode({"q": paper.doi, "qmode": "everything", "limit": 100}))
            existing = [self.paper(i) for i in response.json()
                        if i.get("data", {}).get("DOI", "").casefold() == paper.doi.casefold()]
        if len(existing) > 1:
            raise PaperError("Zotero 中已有多个对应条目，请先用 /paper_find 选择或在 Zotero 整理重复条目。")
        if existing:
            return existing[0], False
        similar = await self.search(paper.title)
        if any(title_key(p.title) == title_key(paper.title) for p in similar):
            raise PaperError("Zotero 存在同名文献但身份信息未匹配，未重复创建。请用 /paper_find 标题核对。")
        key = self.store.stable_key(root + ":paper-assistant:" + identity)
        try:
            item = await self.get_item(key)
        except PaperError as exc:
            if str(exc) != "未找到对应内容。":
                raise
        else:
            found = self.paper(item)
            if base_id(found.arxiv) != base_id(paper.arxiv) or not paper.arxiv:
                raise PaperError("条目编号发生冲突，未覆盖已有文献。")
            return found, False
        collection = str(self.config.get("zotero_collection_key")).strip()
        if collection and not KEY_PATTERN.fullmatch(collection):
            raise PaperError("Zotero 集合编号应为 8 位大写字母或数字。")
        data = {
            "key": key, "itemType": "preprint", "title": paper.title,
            "creators": [{"creatorType": "author", "name": name} for name in paper.authors],
            "abstractNote": paper.abstract, "date": paper.published,
            "repository": "arXiv", "archiveID": paper.arxiv,
            "DOI": paper.doi, "url": self.config.url("arxiv_web_base", f"abs/{paper.arxiv}"),
            "extra": f"arXiv: {paper.arxiv}",
            "tags": [], "collections": [collection] if collection else [],
        }
        return self.paper(await self.create(data)), True

    async def upload_pdf(self, parent: str, paper: Paper, path: Path, *, translated: bool = False) -> str:
        root, _ = self.settings()
        role = "zh" if translated else "original"
        filename = paper.arxiv.replace("/", "_") + f"_{role}.pdf"
        key = self.store.stable_key(root + ":" + parent + ":" + filename)
        # Reuse an existing exact-version attachment without replacing other versions.
        children = await self.children(parent)
        for item in children:
            data = item.get("data", {})
            if data.get("filename") == filename and data.get("contentType") == "application/pdf":
                key = item["key"]
                if data.get("md5"):
                    return key
                break
        else:
            try:
                existing = await self.get_item(key)
            except PaperError as exc:
                if str(exc) != "未找到对应内容。":
                    raise
                await self.create({
                    "key": key, "itemType": "attachment", "parentItem": parent,
                    "linkMode": "imported_file", "title": f"{'中文译文' if translated else '原文'} · {paper.arxiv}",
                    "filename": filename, "contentType": "application/pdf", "tags": [],
                })
            else:
                data = existing.get("data", {})
                if data.get("parentItem") != parent or data.get("filename") != filename:
                    raise PaperError("附件编号冲突，未覆盖已有文件。")
                if data.get("md5"):
                    return key
        content = path.read_bytes()
        digest = hashlib.md5(content).hexdigest()
        auth = (await self.api("POST", f"/items/{key}/file", headers={"If-None-Match": "*"},
                              form={"md5": digest, "filename": filename, "filesize": len(content),
                                    "mtime": int(time.time() * 1000)})).json()
        if auth.get("exists"):
            return key
        required = {"url", "contentType", "prefix", "suffix", "uploadKey"}
        if not required.issubset(auth):
            raise PaperError("Zotero 文件上传授权格式不正确。")
        # The signed storage request must never receive the Zotero API key.
        await self.http.request(
            "POST", auth["url"], headers={"Content-Type": auth["contentType"]},
            data=auth["prefix"].encode() + content + auth["suffix"].encode(),
        )
        await self.api("POST", f"/items/{key}/file", headers={"If-None-Match": "*"},
                       form={"upload": auth["uploadKey"]})
        return key
