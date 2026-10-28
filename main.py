import asyncio
import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain, Reply
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import file_token_service
from astrbot.core.star.filter.command import GreedyStr

from .models import Paper, PaperError, chunks
from .net import Http
from .reading import Reading
from .resolver import Arxiv
from .service import Assistant
from .store import Store
from .zotero import Zotero
from .settings import file_transport, settings

NAME = "astrbot_plugin_paper_assistant"


class PaperAssistantPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = settings(config)
        self.store = None
        self.worker = None

    async def initialize(self):
        self.config.validate()
        self.active_scope = self.config.library_scope()
        directory = StarTools.get_data_dir(NAME) / "libraries" / self.active_scope
        directory.mkdir(parents=True, exist_ok=True)
        self.store = Store(directory / "tasks.sqlite3", self.config)
        http = Http(config=self.config)
        zotero = Zotero(http, self.config, self.store)
        reading = Reading(http, self.store, zotero, directory / "files", self.config)
        self.assistant = Assistant(self.store, Arxiv(http, self.config), zotero, reading, self.config)
        self.worker = asyncio.create_task(self._work())

    @staticmethod
    def _owner(event):
        return json.dumps([event.unified_msg_origin, str(event.get_sender_id())], ensure_ascii=False)

    def _check(self, event):
        allowed = {str(i).strip() for i in self.config.get("allowed_users") if str(i).strip()}
        if not allowed:
            raise PaperError("论文助手尚未配置 allowed_users。请在插件设置填入允许使用的消息平台用户 ID。")
        if str(event.get_sender_id()) not in allowed:
            raise PaperError("你没有使用论文助手的权限。")
        if self.config.get("private_only") and not event.is_private_chat():
            raise PaperError("论文助手当前仅在私聊中提供私人文献服务。")
        if self.store is None:
            raise PaperError("论文助手正在初始化，请稍后重试。")
        if self.active_scope != self.config.library_scope():
            raise PaperError("目标文献库配置已变化，请重载插件后使用。")

    def _reply_paper(self, event):
        owner = self._owner(event)
        for component in event.get_messages():
            if not isinstance(component, Reply):
                continue
            bound = self.store.binding(owner, str(component.id))
            if bound:
                return Paper.load(bound)
            text = component.message_str or ""
            for child in component.chain or []:
                if isinstance(child, Plain):
                    text += child.text
            for key in re.findall(r"\[paper:([a-f0-9]{10})\]", text):
                job = self.store.job(key, owner)
                if job:
                    result = json.loads(job["result"])
                    return Paper.load(result.get("paper") or json.loads(job["paper"]))
        return None

    async def _request(self, event, action, query):
        try:
            self._check(event)
            owner = self._owner(event)
            query = str(query).strip()
            if query:
                papers = await self.assistant.resolve(action, query)
                if not papers:
                    message = "Zotero 中未找到，可使用 /paper_add 添加。" if action == "find" else "未找到对应 arXiv 论文，请使用编号或更完整的英文标题。"
                    yield event.plain_result(message)
                    return
                if self.assistant.needs_choice(action, query, papers):
                    yield event.plain_result(self.assistant.select(owner, action, papers))
                    return
                paper = papers[0]
            else:
                paper = self._reply_paper(event)
                if not paper:
                    raise PaperError("请提供 arXiv 编号或标题，或回复本插件的论文卡片。")
                if action == "find":
                    papers = await self.assistant.resolve("find", paper.arxiv or paper.title)
                    if not papers:
                        raise PaperError("Zotero 中未找到，可使用 /paper_add 添加。")
                    if len(papers) > 1:
                        yield event.plain_result(self.assistant.select(owner, action, papers))
                        return
                    paper = papers[0]
            key, fresh = self.assistant.submit(owner, event.unified_msg_origin, action, paper)
            self.store.bind(owner, str(event.message_obj.message_id), paper.dump())
            yield event.plain_result(f"{'已受理' if fresh else '任务已在处理中'}：{key}\n{paper.title}\n可用 /paper_status {key} 查询。")
        except PaperError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("Paper assistant request failed: {}", type(exc).__name__)
            yield event.plain_result("论文请求失败，请稍后重试；请勿发送账号密钥。")

    @filter.command("paper_add")
    async def paper_add(self, event: AstrMessageEvent, query: GreedyStr):
        """Add an arXiv paper to Zotero."""
        async for result in self._request(event, "add", query):
            yield result

    @filter.command("paper_find")
    async def paper_find(self, event: AstrMessageEvent, query: GreedyStr):
        """Find an existing Zotero paper."""
        async for result in self._request(event, "find", query):
            yield result

    @filter.command("paper_zh")
    async def paper_zh(self, event: AstrMessageEvent, query: GreedyStr):
        """Retrieve a version-labeled translation or the website entry."""
        async for result in self._request(event, "translate", query):
            yield result

    @filter.command("paper_sum")
    async def paper_sum(self, event: AstrMessageEvent, query: GreedyStr):
        """Read an existing Papers.cool explanation."""
        async for result in self._request(event, "summary", query):
            yield result

    @filter.command("paper_sum_model")
    async def paper_sum_model(self, event: AstrMessageEvent, query: GreedyStr):
        """Explicitly request a model summary."""
        async for result in self._request(event, "model", query):
            yield result

    @filter.command("paper_save_zh")
    async def paper_save_zh(self, event: AstrMessageEvent, query: GreedyStr):
        """Save an available translation to Zotero."""
        async for result in self._request(event, "save_translation", query):
            yield result

    @filter.command("paper_pick")
    async def paper_pick(self, event: AstrMessageEvent, query: GreedyStr):
        """Select a candidate belonging to the current user and session."""
        try:
            self._check(event)
            parts = str(query).split()
            if len(parts) != 2 or not parts[1].isdigit():
                raise PaperError("用法：/paper_pick 请求编号 序号")
            owner = self._owner(event)
            action, data = self.store.pick(owner, parts[0], int(parts[1]))
            key, fresh = self.assistant.submit(owner, event.unified_msg_origin, action, Paper.load(data))
            self.store.bind(owner, str(event.message_obj.message_id), data)
            yield event.plain_result(f"{'已受理' if fresh else '任务已在处理中'}：{key}\n{data['title']}")
        except PaperError as exc:
            yield event.plain_result(str(exc))

    @filter.command("paper_status")
    async def paper_status(self, event: AstrMessageEvent, query: GreedyStr):
        """Show a task or retrieve its saved result without rerunning it."""
        try:
            self._check(event)
            job = self.store.job(str(query).strip(), self._owner(event))
            if not job:
                raise PaperError("任务不存在，或不属于当前用户与会话。")
            names = {"queued": "等待中", "running": "处理中", "done": "已处理", "failed": "失败"}
            yield event.plain_result(f"任务 {job['id']}：{names[job['state']]}")
            result = json.loads(job["result"])
            if job["state"] in {"done", "failed"}:
                for text in self._chunks(f"[paper:{job['id']}]\n" + result.get("text", "")):
                    yield event.plain_result(text)
                file = result.get("file")
                if file and Path(file).is_file():
                    yield event.chain_result([await self._file(file)])
        except PaperError as exc:
            yield event.plain_result(str(exc))

    @filter.command("paper_help")
    async def paper_help(self, event: AstrMessageEvent):
        """Show setup and commands."""
        yield event.plain_result(
            "论文助手：\n/paper_add 编号或标题 → 加入 Zotero\n"
            "/paper_find 编号或标题 → 查已有文献\n/paper_zh 编号或标题 → 中文译文\n"
            "/paper_sum 编号或标题 → Papers.cool 已有总结\n"
            "/paper_sum_model 编号或标题 → 模型总结（会调用已配置模型）\n"
            "/paper_save_zh 编号或标题 → 保存已有译文到 Zotero\n"
            "/paper_pick 请求编号 序号\n/paper_status 任务编号\n"
            "可回复论文卡片省略编号。首次请在插件设置填写 allowed_users、"
            "zotero_library_id、zotero_api_key；集合编号可选。\n"
            "幻觉翻译自动创建和下载尚未接通，暂无 Read 或阅读记录功能。"
        )

    async def _file(self, file):
        global_base = self.context.get_config().get("callback_api_base", "")
        mode, base = file_transport(self.config, global_base)
        if mode == "local":
            return File(name=Path(file).name, file=file)
        token = await file_token_service.register_file(file, timeout=self.config.get("file_link_ttl"))
        return File(name=Path(file).name, url=f"{base}/api/file/{token}")

    def _chunks(self, text):
        return chunks(text, self.config.get("message_chunk_chars"))

    async def _model(self, prompt):
        provider_id = str(self.config.get("summary_provider_id")).strip()
        if not provider_id:
            raise PaperError("请在插件设置填写 summary_provider_id，选择已配置的 AstrBot 对话模型。")
        response = await self.context.llm_generate(
            chat_provider_id=provider_id, prompt=prompt,
            system_prompt=self.config.get("summary_system_prompt"),
        )
        return response.completion_text or ""

    def _job_allowed(self, job):
        try:
            origin, sender = json.loads(job["owner"])
            allowed = {str(i).strip() for i in self.config.get("allowed_users")}
            return self.active_scope == self.config.library_scope() and origin == job["origin"] and sender in allowed and (
                not self.config.get("private_only") or ":FriendMessage:" in origin
            )
        except (ValueError, TypeError):
            return False

    async def _deliver(self, job):
        result = json.loads(job["result"])
        try:
            size = result.setdefault("chunk_chars", self.config.get("message_chunk_chars"))
            parts = chunks(f"[paper:{job['id']}]\n" + result.get("text", ""), size)
            start = result.get("sent_parts", 0)
            for index in range(start, len(parts)):
                sent = await self.context.send_message(job["origin"], MessageChain().message(parts[index]))
                if not sent:
                    raise PaperError("当前消息平台尚未连接。")
                result["sent_parts"] = index + 1
                self.store.state(job["id"], job["state"], result)
            file = result.get("file")
            if file and not result.get("file_sent"):
                if not Path(file).is_file():
                    raise PaperError("结果文件已丢失，请重新请求。")
                sent = await self.context.send_message(
                    job["origin"], MessageChain([await self._file(file)])
                )
                if not sent:
                    raise PaperError("文件消息平台尚未连接。")
                result["file_sent"] = True
                self.store.state(job["id"], job["state"], result)
            self.store.delivery(job["id"], True)
        except Exception as exc:
            self.store.delivery(job["id"], False)
            logger.warning("Paper task {} delivery failed: {}", job["id"], type(exc).__name__)

    async def _work(self):
        while True:
            try:
                queued = self.store.queued()
                if queued:
                    job = queued[0]
                    if not self._job_allowed(job):
                        self.store.state(job["id"], "failed", {"text": "授权配置已变化，任务已停止。"})
                        self.store.delivery(job["id"], True)
                        continue
                    self.store.state(job["id"], "running")
                    try:
                        async with asyncio.timeout(self.config.get("job_timeout")):
                            result = await self.assistant.perform(
                                job["action"], Paper.load(json.loads(job["paper"])),
                                model=self._model,
                                checkpoint=lambda r: self.store.state(job["id"], "running", r),
                            )
                        self.store.state(job["id"], "done", result)
                    except Exception as exc:
                        text = str(exc) if isinstance(exc, PaperError) else (
                            "任务超时，请查询 Zotero 后重试。" if isinstance(exc, TimeoutError)
                            else "论文处理失败，请稍后重试。"
                        )
                        previous = json.loads(self.store.job(job["id"])["result"])
                        previous["text"] = previous.get("text", "") + "\n" + text
                        self.store.state(job["id"], "failed", previous)
                        logger.warning("Paper task {} failed: {}", job["id"], type(exc).__name__)
                for job in self.store.undelivered():
                    if self._job_allowed(job):
                        await self._deliver(job)
                await asyncio.sleep(self.config.get("worker_interval"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Paper worker failed: {}", type(exc).__name__)
                await asyncio.sleep(self.config.get("worker_interval"))

    async def terminate(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        if self.store:
            self.store.close()
