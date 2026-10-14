import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "tg_nas_adapter_test",
    Path(__file__).resolve().parents[1] / "bridge_adapter.py",
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def test_private_whitelist_rejects_groups_and_other_users():
    payload = {
        "sender_id": "123",
        "chat_id": "123",
        "chat_type": "private",
        "request_id": "123-1",
        "text": "/订阅列表",
    }
    adapter.validate_private_message(payload, {"123"})
    for update in (
        {"chat_id": "-100123", "chat_type": "supergroup"},
        {"sender_id": "456"},
        {"request_id": ""},
        {"text": "x" * 16001},
    ):
        with pytest.raises(ValueError):
            adapter.validate_private_message({**payload, **update}, {"123"})


def test_incoming_media_paths_cannot_escape_the_share(tmp_path):
    (tmp_path / "test.png").write_bytes(b"image")
    assert adapter.shared_media_path(tmp_path, "test.png") == tmp_path / "test.png"
    for name in ("../file", "..\\file", "folder/file", "/etc/passwd", "missing"):
        with pytest.raises(ValueError):
            adapter.shared_media_path(tmp_path, name)


@pytest.mark.asyncio
async def test_text_delivery_retries_use_the_same_id(monkeypatch):
    error = adapter.aiohttp.ClientResponseError(None, (), status=503)
    responses = []
    for failure in (error, None):
        response = SimpleNamespace(
            status=503 if failure else 200,
            json=AsyncMock(return_value={"ok": True}),
            raise_for_status=Mock(side_effect=failure),
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = response
        responses.append(manager)
    instance = adapter.NasTelegramAdapter.__new__(adapter.NasTelegramAdapter)
    instance.whitelist = {"123"}
    instance.secret = "a" * 40
    instance.nas_url = "http://nas"
    instance.http = SimpleNamespace(post=Mock(side_effect=responses))
    monkeypatch.setattr(adapter.asyncio, "sleep", AsyncMock())
    assert (await instance.deliver("123", [{"type": "text", "text": "result"}]))["ok"]
    calls = instance.http.post.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["json"]["delivery_id"] == calls[1].kwargs["json"]["delivery_id"]


def test_scheduled_command_uses_the_bridge_event():
    instance = adapter.NasTelegramAdapter.__new__(adapter.NasTelegramAdapter)
    instance.metadata = adapter.PlatformMetadata(name="telegram", description="test", id="nas_tg")
    message = adapter.AstrBotMessage()
    message.type = adapter.MessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = "123"
    message.message_str = "/fortune"
    message.sender = adapter.MessageMember(user_id="123", nickname="test")
    event = instance.create_event(message)
    assert isinstance(event, adapter.NasTelegramEvent)
    assert event.request_id == ""
