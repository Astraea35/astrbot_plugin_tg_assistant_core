"""Telegram transport that keeps polling and media delivery on the NAS."""

from __future__ import annotations

import asyncio
import hmac
import mimetypes
import secrets
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.star import StarTools

BRIDGE_TYPE = "telegram_nas"


def validate_private_message(payload: dict, whitelist: set[str]) -> None:
    sender = str(payload.get("sender_id", ""))
    chat = str(payload.get("chat_id", ""))
    if (
        sender not in whitelist
        or chat != sender
        or payload.get("chat_type") != "private"
    ):
        raise ValueError("Only whitelisted private Telegram chats are accepted")
    request_id = str(payload.get("request_id", ""))
    if not request_id or len(request_id) > 100:
        raise ValueError("Invalid request ID")
    if len(str(payload.get("text", ""))) > 16000:
        raise ValueError("Message is too long")


def shared_media_path(root: Path, filename: str) -> Path:
    if (
        not filename
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("Invalid media filename")
    resolved_root = root.resolve()
    path = (resolved_root / filename).resolve()
    if not path.is_relative_to(resolved_root) or not path.is_file():
        raise ValueError("Media is outside the bridge input directory")
    return path


class NasTelegramEvent(AstrMessageEvent):
    def __init__(
        self, message: AstrBotMessage, adapter: "NasTelegramAdapter", request_id: str
    ):
        super().__init__(
            message.message_str,
            message,
            adapter.meta(),
            message.session_id,
        )
        self.adapter = adapter
        self.request_id = request_id
        self.output_count = 0
        self._finished = False
        self.set_extra("nas_telegram_bridge", True)

    async def send(self, message: MessageChain | None) -> None:
        if message is None:
            return
        parts = await self.adapter.serialize_chain(message.chain)
        if parts:
            await self.adapter.deliver(
                self.session_id,
                parts,
                request_id=self.request_id,
                kind="status" if self.get_extra("nas_bridge_status") else "result",
            )
            self.output_count += 1
        await super().send(MessageChain([]))

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        accumulated = ""
        async for chain in generator:
            for component in chain.chain:
                if isinstance(component, Plain):
                    accumulated += component.text
            if accumulated:
                await self.send(MessageChain([Plain(accumulated)]))

    async def send_typing(self) -> None:
        await self.adapter.deliver(
            self.session_id,
            [],
            request_id=self.request_id,
            kind="typing",
        )

    def cleanup_temporary_local_files(self) -> None:
        super().cleanup_temporary_local_files()
        if not self._finished:
            self._finished = True
            self.adapter.background_task(
                self.adapter.deliver(
                    self.session_id,
                    [],
                    request_id=self.request_id,
                    kind="complete",
                    had_output=bool(self.output_count),
                ),
            )


@register_platform_adapter(
    BRIDGE_TYPE,
    "Telegram via an authenticated NAS downloader",
    default_config_tmpl={
        "id": "nas_tg",
        "enable": False,
        "host": "0.0.0.0",
        "port": 8769,
        "nas_url": "",
        "shared_secret": "",
        "allowed_user_ids": [],
        "nas_input_share": "",
        "public_url": "",
    },
    adapter_display_name="Telegram (NAS)",
    support_streaming_message=False,
)
class NasTelegramAdapter(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue):
        super().__init__(platform_config, event_queue)
        self.metadata = PlatformMetadata(
            name="telegram",
            description="Telegram transport hosted on the NAS",
            id=platform_config["id"],
            support_streaming_message=False,
            support_proactive_message=True,
            adapter_display_name="Telegram (NAS)",
        )
        self.secret = str(platform_config.get("shared_secret", ""))
        self.whitelist = {
            str(uid) for uid in platform_config.get("allowed_user_ids", [])
        }
        self.input_root = Path(platform_config.get("nas_input_share", "."))
        self.public_url = str(platform_config.get("public_url", "")).rstrip("/")
        self.nas_url = str(platform_config.get("nas_url", "")).rstrip("/")
        self.client_self_id = str(platform_config.get("bot_id", "nas_tg_bot"))
        self.assets_root = (
            StarTools.get_data_dir("astrbot_plugin_tg_assistant_core") / "outbox"
        )
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.assets: dict[str, tuple[Path, float]] = {}
        self.requests: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._shutdown = asyncio.Event()
        self.http: aiohttp.ClientSession | None = None
        self.runner: web.AppRunner | None = None

    def meta(self) -> PlatformMetadata:
        return self.metadata

    def get_client(self):
        return self

    def background_task(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def finished(done):
            self._tasks.discard(done)
            if not done.cancelled() and (error := done.exception()):
                logger.error("NAS Telegram delivery failed: %s", type(error).__name__)

        task.add_done_callback(finished)

    @web.middleware
    async def authenticate(self, request: web.Request, handler):
        supplied = request.headers.get("X-Bridge-Token", "")
        if len(self.secret) < 32 or not hmac.compare_digest(supplied, self.secret):
            raise web.HTTPUnauthorized()
        return await handler(request)

    def _incoming_chain(self, payload: dict) -> list:
        chain: list = []
        if text := str(payload.get("text", "")):
            chain.append(Plain(text))
        media_items = payload.get("media", [])
        if not isinstance(media_items, list) or len(media_items) > 10:
            raise ValueError("Invalid media list")
        for media in media_items:
            if not isinstance(media, dict):
                raise ValueError("Invalid media entry")
            path = shared_media_path(self.input_root, str(media.get("filename", "")))
            media_type = media.get("type")
            if media_type == "image":
                chain.append(Image.fromFileSystem(str(path)))
            elif media_type == "record":
                chain.append(Record.fromFileSystem(str(path)))
            elif media_type == "video":
                chain.append(Video.fromFileSystem(str(path)))
            elif media_type == "file":
                chain.append(
                    File(name=str(media.get("name", path.name)), file=str(path))
                )
            else:
                raise ValueError("Unsupported incoming media type")
        return chain

    async def receive(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            validate_private_message(payload, self.whitelist)
            now = time.monotonic()
            self.requests = {
                key: stamp for key, stamp in self.requests.items() if now - stamp < 3600
            }
            request_id = payload["request_id"]
            if request_id in self.requests:
                return web.json_response({"ok": True, "duplicate": True})
            if len(self.requests) >= 2000:
                raise web.HTTPTooManyRequests()
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.self_id = self.client_self_id
            message.session_id = str(payload["chat_id"])
            message.message_id = str(payload.get("message_id", request_id))
            message.sender = MessageMember(
                user_id=str(payload["sender_id"]),
                nickname=str(payload.get("sender_name", "Telegram")),
            )
            message.message_str = str(payload.get("text", ""))
            message.message = self._incoming_chain(payload)
            if reply := payload.get("reply"):
                message.message.insert(
                    0,
                    Reply(
                        id=str(reply.get("message_id", "")),
                        sender_id=str(reply.get("sender_id", "")),
                        sender_nickname=str(reply.get("sender_name", "")),
                        message_str=str(reply.get("text", "")),
                        chain=self._incoming_chain(reply),
                    ),
                )
            message.raw_message = payload
            self.requests[request_id] = now
            self.commit_event(NasTelegramEvent(message, self, request_id))
            return web.json_response({"ok": True, "request_id": request_id})
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text="Invalid or unauthorized bridge message")

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "platform_id": self.meta().id,
                "private_only": True,
                "whitelist_count": len(self.whitelist),
            }
        )

    async def file(self, request: web.Request) -> web.Response:
        key = request.match_info["asset_id"]
        entry = self.assets.get(key)
        if not entry or entry[1] < time.monotonic() or not entry[0].is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(entry[0])

    async def serialize_chain(self, chain: list) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for component in chain:
            if isinstance(component, Plain):
                parts.append({"type": "text", "text": component.text})
            elif isinstance(component, (Node, Nodes)):
                if isinstance(component, Nodes):
                    for node in component.nodes:
                        parts.extend(await self.serialize_chain(node.content))
                else:
                    parts.extend(await self.serialize_chain(component.content))
            elif isinstance(component, (Image, Video, Record, File)):
                path = Path(
                    await component.get_file()
                    if isinstance(component, File)
                    else await component.convert_to_file_path()
                )
                asset_id = secrets.token_urlsafe(24)
                destination = self.assets_root / (asset_id + path.suffix)
                await asyncio.to_thread(shutil.copyfile, path, destination)
                self.assets[asset_id] = (destination, time.monotonic() + 900)
                media_type = (
                    "image"
                    if isinstance(component, Image)
                    else "video"
                    if isinstance(component, Video)
                    else "record"
                    if isinstance(component, Record)
                    else "file"
                )
                if media_type == "image" and path.suffix.lower() == ".gif":
                    media_type = "animation"
                parts.append(
                    {
                        "type": media_type,
                        "asset_id": asset_id,
                        "name": str(getattr(component, "name", "") or path.name),
                        "mime_type": mimetypes.guess_type(str(path))[0],
                    }
                )
        return parts

    async def deliver(
        self,
        chat_id: str,
        parts: list,
        *,
        request_id: str = "",
        kind: str = "result",
        had_output: bool = True,
    ) -> dict:
        if str(chat_id) not in self.whitelist:
            raise ValueError("Delivery target is outside the Telegram whitelist")
        if self.http is None:
            raise RuntimeError("Bridge is not running")
        payload = {
            "chat_id": str(chat_id),
            "request_id": request_id,
            "kind": kind,
            "parts": parts,
            "had_output": had_output,
            "delivery_id": secrets.token_urlsafe(24),
        }
        async with self.http.post(
            self.nas_url + "/api/astrbot/push",
            json=payload,
            headers={"X-Bridge-Token": self.secret},
        ) as response:
            response.raise_for_status()
            result = await response.json()
            if not result.get("ok"):
                raise RuntimeError("NAS rejected a Telegram delivery")
            return result

    async def send_by_session(self, session, message_chain: MessageChain) -> None:
        parts = await self.serialize_chain(message_chain.chain)
        if parts:
            await self.deliver(session.session_id, parts)
        await super().send_by_session(session, message_chain)

    async def run(self) -> None:
        if (
            len(self.secret) < 32
            or not self.whitelist
            or not self.nas_url
            or not self.public_url
            or not self.config.get("nas_input_share")
        ):
            raise ValueError("Set a shared secret, whitelist and both bridge URLs")
        for url in (self.nas_url, self.public_url):
            if urlparse(url).scheme not in {"http", "https"}:
                raise ValueError("Bridge URLs must use HTTP or HTTPS")
        self.http = aiohttp.ClientSession(
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=180),
        )
        application = web.Application(
            middlewares=[self.authenticate], client_max_size=128 * 1024
        )
        application.router.add_get("/health", self.health)
        application.router.add_post("/message", self.receive)
        application.router.add_get("/files/{asset_id}", self.file)
        self.runner = web.AppRunner(application, access_log=None)
        await self.runner.setup()
        await web.TCPSite(
            self.runner,
            self.config.get("host", "0.0.0.0"),
            int(self.config.get("port", 8769)),
        ).start()
        logger.info(
            "NAS Telegram bridge started on port %s", self.config.get("port", 8769)
        )
        self.background_task(self._expire_assets())
        await self._shutdown.wait()

    async def _expire_assets(self) -> None:
        while not self._shutdown.is_set():
            now = time.monotonic()
            for key, (path, deadline) in list(self.assets.items()):
                if deadline < now:
                    self.assets.pop(key, None)
                    path.unlink(missing_ok=True)
            await asyncio.sleep(60)

    async def terminate(self) -> None:
        self._shutdown.set()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        if self.http:
            await self.http.close()
