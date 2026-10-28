import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PACKAGE = ROOT.name
models = importlib.import_module(PACKAGE + ".models")
net = importlib.import_module(PACKAGE + ".net")
Store = importlib.import_module(PACKAGE + ".store").Store
Arxiv = importlib.import_module(PACKAGE + ".resolver").Arxiv
Zotero = importlib.import_module(PACKAGE + ".zotero").Zotero
Reading = importlib.import_module(PACKAGE + ".reading").Reading
Assistant = importlib.import_module(PACKAGE + ".service").Assistant
Paper, PaperError = models.Paper, models.PaperError

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Attention Is
All You Need</title><summary>Transformer abstract</summary><published>2017-06-12T00:00:00Z</published>
<author><name>Ashish Vaswani</name></author><category term="cs.CL"/><x:doi>10.example/test</x:doi></entry>
</feed>"""


def response(data=None, body=None, headers=None):
    return net.Response(200, body if body is not None else json.dumps(data).encode(), headers or {})


def item(key="ABCDEFGH", identifier="1706.03762v7", **extra):
    return {"key": key, "data": {
        "itemType": "preprint", "title": "Attention Is All You Need",
        "archiveID": identifier, "url": "", "creators": [], **extra,
    }}


class ModelsTests(unittest.TestCase):
    def test_modern_old_and_links(self):
        for value, expected in [
            ("1706.03762", "1706.03762"),
            ("arXiv:1706.03762v7", "1706.03762v7"),
            ("https://arxiv.org/pdf/1706.03762v7.pdf?download=1", "1706.03762v7"),
            ("https://arxiv.org/abs/hep-th/9901001v2", "hep-th/9901001v2"),
            ("math.GT/0309136", "math.GT/0309136"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(models.arxiv_id(value), expected)

    def test_reject_arbitrary_url_and_invalid_id(self):
        for value in ["https://example.org/abs/1706.03762", "../1706.03762",
                      "1706.03762v0", "1706.03762v7oops", "https://arxiv.org.evil.test/pdf/1706.03762"]:
            self.assertEqual(models.arxiv_id(value), "")

    def test_extra_arxiv_and_title_normalization(self):
        paper = models.from_zotero(item(identifier="", extra="arXiv: 1706.03762v7"))
        self.assertEqual(paper.arxiv, "1706.03762v7")
        self.assertEqual(models.title_key("Attention: Is ALL You Need!"),
                         models.title_key("Attention Is All You Need"))

    def test_chunks_preserve_text(self):
        text = "论" * 3400
        self.assertEqual("".join(models.chunks(text)), text)
        self.assertLessEqual(max(map(len, models.chunks(text))), 1600)

    def test_atom_and_error_entry(self):
        papers = Arxiv.parse(ATOM)
        self.assertEqual(papers[0].title, "Attention Is All You Need")
        self.assertEqual(papers[0].arxiv, "1706.03762v7")
        self.assertEqual(papers[0].authors, ["Ashish Vaswani"])
        self.assertEqual(Arxiv.parse(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>error</id></entry></feed>'), [])

    def test_entity_xml_rejected(self):
        with self.assertRaises(PaperError):
            Arxiv.parse(b'<!DOCTYPE x [<!ENTITY a "oops">]><feed/>')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_candidate_owner_and_expiry(self):
        key = self.store.choice("alice", "add", [{"title": "A"}])
        with self.assertRaises(PaperError):
            self.store.pick("bob", key, 1)
        with self.assertRaises(PaperError):
            self.store.pick("alice", key, 0)
        self.assertEqual(self.store.pick("alice", key, 1)[0], "add")
        self.store.db.execute("UPDATE choices SET expires=0")
        with self.assertRaises(PaperError):
            self.store.pick("alice", key, 1)

    def test_pending_requests_deduplicated_and_isolated(self):
        key, fresh = self.store.enqueue("alice", "origin", "add", {"title": "A"})
        self.assertTrue(fresh)
        self.assertEqual(self.store.enqueue("alice", "origin", "add", {"title": "A"}), (key, False))
        self.assertIsNone(self.store.job(key, "bob"))

    def test_restart_recovers_running_and_keeps_completed(self):
        key, _ = self.store.enqueue("alice", "origin", "add", {"title": "A"})
        other, _ = self.store.enqueue("alice", "origin", "summary", {"title": "A"})
        self.store.state(key, "running", {"text": "metadata saved"})
        self.store.state(other, "done", {"text": "summary"})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.job(key)["state"], "queued")
        self.assertEqual(self.store.job(other)["state"], "done")
        self.assertEqual(json.loads(self.store.job(key)["result"])["text"], "metadata saved")

    def test_delivery_retries_are_bounded(self):
        key, _ = self.store.enqueue("alice", "origin", "summary", {"title": "A"})
        self.store.state(key, "done", {"text": "summary"})
        for _ in range(3):
            self.assertEqual(len(self.store.undelivered()), 1)
            self.store.delivery(key, False)
        self.assertEqual(self.store.undelivered(), [])

    def test_binding_scope_and_cache(self):
        self.store.bind("alice", "message", {"title": "A"})
        self.assertIsNone(self.store.binding("bob", "message"))
        self.store.put("test", {"a": 1})
        self.assertEqual(self.store.get("test"), {"a": 1})


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.config = {"zotero_library_id": "123", "zotero_api_key": "test-secret"}
        self.http = types.SimpleNamespace(request=AsyncMock())
        self.zotero = Zotero(self.http, self.config, self.store)
        self.reading = Reading(self.http, self.store, self.zotero, self.root / "files", self.config)
        self.assistant = Assistant(self.store, Arxiv(self.http), self.zotero, self.reading, self.config)
        self.paper = Paper("Attention Is All You Need", arxiv="1706.03762v7")
        self.pdf = self.reading.save_pdf(self.root / "test.pdf", b"%PDF-1.7\n" + b"x" * 200)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_arxiv_requested_version_must_match(self):
        self.http.request.return_value = response(body=ATOM)
        with self.assertRaisesRegex(PaperError, "指定版本"):
            await self.assistant.arxiv.resolve("1706.03762v1")

    async def test_title_always_needs_choice(self):
        self.assertTrue(self.assistant.needs_choice("add", self.paper.title, [self.paper]))
        self.assertFalse(self.assistant.needs_choice("add", self.paper.arxiv, [self.paper]))

    async def test_zotero_id_filter_ignores_other_versions_and_children(self):
        self.http.request.return_value = response([item(), item("HIJKLMNP", "1901.00001")])
        found = await self.zotero.search("1706.03762v1")
        self.assertEqual([p.zotero_key for p in found], ["ABCDEFGH"])

    async def test_find_is_read_only(self):
        self.http.request.return_value = response([])
        await self.assistant.resolve("find", "1706.03762")
        self.assertTrue(all(c.args[0] == "GET" for c in self.http.request.call_args_list))

    async def test_zotero_parent_reused(self):
        self.zotero.search = AsyncMock(return_value=[models.from_zotero(item())])
        self.zotero.create = AsyncMock()
        found, created = await self.zotero.ensure_paper(self.paper)
        self.assertFalse(created)
        self.assertEqual(found.zotero_key, "ABCDEFGH")
        self.zotero.create.assert_not_called()

    async def test_same_title_without_identity_blocks_duplicate_creation(self):
        existing = Paper(self.paper.title, zotero_key="ABCDEFGH")
        self.zotero.search = AsyncMock(side_effect=[[], [existing]])
        self.zotero.create = AsyncMock()
        with self.assertRaisesRegex(PaperError, "同名"):
            await self.zotero.ensure_paper(self.paper)
        self.zotero.create.assert_not_called()

    async def test_translation_without_credentials_returns_honest_entry(self):
        self.config['zotero_api_key'] = ''
        path, text = await self.reading.translated(self.paper)
        self.assertEqual(path, "")
        self.assertIn("hjfy.top", text)
        self.assertIn("没有创建", text)
        self.http.request.assert_not_called()


    async def test_new_preprint_creation_fields(self):
        self.zotero.search = AsyncMock(return_value=[])
        self.zotero.get_item = AsyncMock(side_effect=PaperError("未找到对应内容。"))
        self.zotero.create = AsyncMock(return_value=item())
        found, created = await self.zotero.ensure_paper(self.paper)
        self.assertTrue(created)
        data = self.zotero.create.call_args.args[0]
        self.assertEqual(data["itemType"], "preprint")
        self.assertEqual(data["archiveID"], self.paper.arxiv)
        self.assertEqual(data["tags"], [])
        self.assertEqual(found.zotero_key, "ABCDEFGH")

    async def test_unconfigured_model_never_downloads_or_calls_model(self):
        self.reading.model_material = AsyncMock()
        model = AsyncMock()
        with self.assertRaises(PaperError):
            await self.assistant.perform("model", self.paper, model=model)
        self.reading.model_material.assert_not_called()
        model.assert_not_called()

    async def test_multiple_existing_papers_do_not_write(self):
        self.zotero.search = AsyncMock(return_value=[self.paper, self.paper])
        self.zotero.create = AsyncMock()
        with self.assertRaisesRegex(PaperError, "多个"):
            await self.zotero.ensure_paper(self.paper)
        self.zotero.create.assert_not_called()

    async def test_stable_key_recovers_unknown_post_result(self):
        self.zotero.search = AsyncMock(return_value=[])
        self.zotero.get_item = AsyncMock(return_value=item())
        self.zotero.create = AsyncMock()
        found, created = await self.zotero.ensure_paper(self.paper)
        self.assertFalse(created)
        self.zotero.create.assert_not_called()
        self.assertEqual(found.arxiv, self.paper.arxiv)

    async def test_quota_failure_preserves_metadata_and_original_file(self):
        self.zotero.ensure_paper = AsyncMock(return_value=(models.from_zotero(item()), True))
        self.reading.pdf = AsyncMock(return_value=self.pdf)
        self.zotero.upload_pdf = AsyncMock(side_effect=PaperError("存储空间不足"))
        saved = []
        result = await self.assistant.perform("add", self.paper, checkpoint=lambda r: saved.append(dict(r)))
        self.assertIn("已加入", saved[0]["text"])
        self.assertIn("尚未同步", result["text"])
        self.assertEqual(result["file"], str(self.pdf))

    async def test_zotero_storage_upload_protocol_and_key_isolation(self):
        self.zotero.children = AsyncMock(return_value=[])
        self.zotero.get_item = AsyncMock(side_effect=PaperError("未找到对应内容。"))
        self.http.request.side_effect = [
            response({"successful": {"0": item("ZZZZZZZZ")}}),
            response({"url": "https://s3.example.org/upload", "contentType": "multipart/form-data; boundary=a",
                      "prefix": "prefix", "suffix": "suffix", "uploadKey": "upload123"}),
            response(body=b""),
            response(body=b""),
        ]
        await self.zotero.upload_pdf("ABCDEFGH", self.paper, self.pdf)
        calls = self.http.request.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1].kwargs["headers"]["If-None-Match"], "*")
        self.assertNotIn("Zotero-API-Key", calls[2].kwargs["headers"])
        self.assertEqual(calls[2].kwargs["data"], b"prefix" + self.pdf.read_bytes() + b"suffix")
        self.assertEqual(calls[3].kwargs["form"], {"upload": "upload123"})

    async def test_existing_attachment_skips_upload(self):
        self.zotero.children = AsyncMock(return_value=[{
            "key": "ABCDEFGH", "data": {"filename": "1706.03762v7_original.pdf",
            "contentType": "application/pdf", "md5": "abc"},
        }])
        self.assertEqual(await self.zotero.upload_pdf("HIJKLMNP", self.paper, self.pdf), "ABCDEFGH")
        self.http.request.assert_not_called()

    async def test_pdf_html_rejected(self):
        self.http.request.return_value = response(body=b"<html>login</html>")
        with self.assertRaisesRegex(PaperError, "PDF"):
            await self.reading.pdf(self.paper)
        self.assertFalse(self.reading.path(self.paper, "original").exists())

    async def test_summary_only_reads_existing_and_caches(self):
        body = ("已有解读内容。" * 40).encode()
        self.http.request.return_value = response(body=body, headers={"Content-Type": "text/plain"})
        result = await self.reading.summary(self.paper)
        self.assertIn("Papers.cool", result)
        self.assertIn("未声明", result)
        await self.reading.summary(self.paper)
        self.assertEqual(self.http.request.await_count, 1)
        self.assertEqual(self.http.request.call_args.args[0], "GET")

    async def test_summary_failure_does_not_silently_call_model(self):
        self.http.request.side_effect = PaperError("未找到对应内容。")
        result = await self.reading.summary(self.paper)
        self.assertIn("/paper_sum_model", result)
        self.assertEqual(self.http.request.await_count, 1)

    async def test_translation_rejects_wrong_version(self):
        self.paper.zotero_key = "ABCDEFGH"
        self.zotero.children = AsyncMock(return_value=[{
            "key": "HIJKLMNP", "data": {"title": "中文译文 1706.03762v70",
            "contentType": "application/pdf", "md5": "abc"},
        }])
        path, text = await self.reading.translated(self.paper)
        self.assertEqual(path, "")
        self.assertIn("尚未接通", text)
        self.http.request.assert_not_called()

    async def test_model_summary_explicit_basis_and_cache(self):
        self.config["summary_provider_id"] = "configured-model"
        self.reading.model_material = AsyncMock(return_value=("abstract only", "仅论文摘要"))
        model = AsyncMock(return_value="研究问题：演示")
        result = await self.assistant.perform("model", self.paper, model=model)
        self.assertIn("仅论文摘要", result["text"])
        await self.assistant.perform("model", self.paper, model=model)
        self.assertEqual(model.await_count, 1)


class RedirectTests(unittest.TestCase):
    def test_cross_origin_redirect_strips_zotero_key(self):
        req = Request("https://api.zotero.org/users/123/items/ABCDEFGH/file",
                      headers={"Zotero-API-Key": "secret", "Authorization": "secret"})
        redirected = net.SafeRedirect().redirect_request(
            req, None, 302, "", {}, "https://s3.example.org/file"
        )
        self.assertFalse(any(k.lower() in {"zotero-api-key", "authorization"} for k in redirected.headers))

    def test_insecure_redirect_rejected(self):
        with self.assertRaises(PaperError):
            net.SafeRedirect().redirect_request(
                Request("https://arxiv.org/pdf/1706.03762"), None, 302, "", {},
                "http://arxiv.org/file",
            )


if __name__ == "__main__":
    unittest.main()
