import importlib.util
from pathlib import Path

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
