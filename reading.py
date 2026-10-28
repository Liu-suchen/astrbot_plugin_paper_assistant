from __future__ import annotations

import asyncio
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

from .models import Paper, PaperError, base_id
from .net import Http
from .store import Store
from .zotero import Zotero
from .settings import settings


class Reading:
    def __init__(self, http: Http, store: Store, zotero: Zotero, directory: Path, config):
        self.http, self.store, self.zotero = http, store, zotero
        self.directory, self.config = directory, settings(config)
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, paper: Paper, role: str) -> Path:
        if not paper.arxiv:
            raise PaperError("该文献没有 arXiv 编号，首版暂不支持此阅读操作。")
        return self.directory / f"{paper.arxiv.replace('/', '_')}_{role}.pdf"

    async def pdf(self, paper: Paper) -> Path:
        path = self.path(paper, "original")
        if path.exists() and path.read_bytes()[:1024].find(b"%PDF-") >= 0:
            return path
        response = await self.http.request("GET", self.config.url("arxiv_web_base", f"pdf/{paper.arxiv}"), limit=self.config.get("max_pdf_mb") * 1024 * 1024)
        return self.save_pdf(path, response.body)

    @staticmethod
    def save_pdf(path: Path, body: bytes) -> Path:
        if b"%PDF-" not in body[:1024] or len(body) < 100:
            raise PaperError("下载结果不是有效的 PDF 文件，未保存。")
        temporary = path.with_suffix(".part")
        temporary.write_bytes(body)
        temporary.replace(path)
        return path

    async def translated(self, paper: Paper) -> tuple[str, str]:
        path = self.path(paper, "zh")
        if path.exists() and b"%PDF-" in path.read_bytes()[:1024]:
            return str(path), f"中文译文缓存 · {paper.arxiv}"
        # Reuse user-owned Zotero translations. Do not guess a private hjfy endpoint.
        matches = [paper] if paper.zotero_key else []
        lookup_note = ""
        if not matches and self.config.get("zotero_api_key") and self.config.get("zotero_library_id"):
            try:
                matches = await self.zotero.search(paper.arxiv)
            except PaperError as exc:
                lookup_note = f"Zotero 译文查询未完成：{exc}\n"

        if len(matches) > 1:
            raise PaperError("Zotero 中匹配到多个条目，请用 /paper_find 选择正确条目后回复 /paper_zh。")
        if matches:
            children = await self.zotero.children(matches[0].zotero_key)
            candidates = []
            for item in children:
                data = item.get("data", {})
                label = data.get("title", "") + " " + data.get("filename", "")
                if data.get("contentType") != "application/pdf" or not data.get("md5"):
                    continue
                if not any(str(word).lower() in label.lower() for word in self.config.get("translation_markers")):
                    continue
                # Only accept an explicitly labeled matching version.
                normalized = label.replace("_", "/")
                if not re.search(re.escape(paper.arxiv) + r"(?![0-9v])", normalized):
                    continue
                candidates.append(item)
            if len(candidates) == 1:
                response = await self.zotero.api(
                    "GET", f"/items/{candidates[0]['key']}/file", limit=self.config.get("max_pdf_mb") * 1024 * 1024
                )
                self.save_pdf(path, response.body)
                return str(path), f"Zotero 已有中文译文 · {paper.arxiv}"
        return "", (
            lookup_note + f"尚无可确认版本的中文译文 PDF：{paper.arxiv}\n"
            f'翻译网站：{self.config.get("translation_site_url")}\n请在网站输入 {paper.arxiv}。\n'
            "网站自动提交、进度查询和下载尚未接通；这里没有创建翻译任务或产生翻译费用。\n"
            "可使用 /paper_sum 获取已有总结，或 /paper_sum_model 生成模型总结。"
        )

    async def summary(self, paper: Paper) -> str:
        if not paper.arxiv:
            raise PaperError("该条目没有 arXiv 编号，无法定位 Papers.cool 页面。")
        cache_key = "cool:" + self.config.fingerprint(["papers_cool_base", "max_summary_chars"]) + ":" + paper.arxiv
        cached = self.store.get(cache_key)
        if cached:
            return cached
        page = self.config.url("papers_cool_base", "arxiv/" + base_id(paper.arxiv))
        # The site's copyKimi() uses this GET endpoint for an existing explanation.
        # POST would request generation and is deliberately not used here.
        try:
            response = await self.http.request(
                "GET", self.config.url("papers_cool_base", "arxiv/kimi") + "?" + urlencode({"paper": base_id(paper.arxiv)})
            )
            body = response.body.decode("utf-8").strip()
            content_type = response.headers.get("Content-Type", response.headers.get("content-type", ""))
            if "html" not in content_type and len(body) > 100 and not body.lstrip().startswith(("<", "{")):
                result = (
                    f"来源：Papers.cool 已有解读\n{page}\n"
                    "网站解读未声明对应 arXiv 版本，可能与指定版本不同。\n\n" + body[:self.config.get("max_summary_chars")]
                )
                self.store.put(cache_key, result)
                return result
        except PaperError:
            pass
        return (
            f"暂未取得 Papers.cool 已有解读。\n阅读入口：{page}\n"
            f"论文：{paper.title}\n"
            "如需使用已配置模型，请输入 /paper_sum_model " + paper.arxiv
        )

    async def model_material(self, paper: Paper) -> tuple[str, str]:
        try:
            path = await self.pdf(paper)

            def extract():
                from pypdf import PdfReader
                reader = PdfReader(BytesIO(path.read_bytes()))
                text, size = [], 0
                for page in reader.pages:
                    value = page.extract_text() or ""
                    text.append(value)
                    size += len(value)
                    if size >= self.config.get("max_pdf_chars"):
                        break
                return "\n".join(text)[:self.config.get("max_pdf_chars")]

            text = await asyncio.to_thread(extract)
            if len(text.strip()) > 500:
                return text, f'原文 PDF 提取文本（最多 {self.config.get("max_pdf_chars"):,} 字符，可能未覆盖全文）'
        except Exception:
            # PDF parsers may raise provider-specific errors; fall back to the abstract.
            pass
        if paper.abstract:
            return paper.abstract, "仅论文摘要"
        raise PaperError("无法取得可用的 PDF 文本或摘要，未调用模型。")
