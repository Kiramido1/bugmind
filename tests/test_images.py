"""Screenshot attachments: validation, provider wiring, storage, CLI and TUI."""

import base64
import json
import sys

import pytest
from typer.testing import CliRunner

from bugmind.cli import app as cli
from bugmind.config import ClaudeConfig, OpenCodeConfig, load_config
from bugmind.images import MAX_IMAGE_BYTES, ImageError, extract_images, load_image
from bugmind.providers import ProviderRequest
from bugmind.providers.claude import ClaudeCodeProvider
from bugmind.providers.opencode import OpenCodeProvider

from conftest import FAKE_AGENT

runner = CliRunner()
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def shots(tmp_path):
    (tmp_path / "err.png").write_bytes(PNG)
    (tmp_path / "with space.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    (tmp_path / "fake.png").write_text("#!/bin/sh\necho not an image\n")
    return tmp_path


def custom_agent(record):
    assert runner.invoke(cli, ["config", "set", "agent", "custom"]).exit_code == 0
    assert runner.invoke(cli, ["config", "set", "provider.custom.command", f"['{sys.executable}', '{FAKE_AGENT}']"]).exit_code == 0
    import os
    os.environ["FAKE_AGENT_RECORD"] = str(record)


def test_images_are_validated_by_content_and_size(shots, tmp_path):
    assert load_image(shots / "err.png").media_type == "image/png"
    assert load_image(shots / "with space.jpg").media_type == "image/jpeg"
    with pytest.raises(ImageError, match="not a PNG"):
        load_image(shots / "fake.png")  # extension lies
    with pytest.raises(ImageError, match="not found"):
        load_image(tmp_path / "missing.png")
    big = tmp_path / "big.png"
    big.write_bytes(PNG[:8] + b"0" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(ImageError, match="limit"):
        load_image(big)


def test_dragged_paths_are_extracted_but_url_paths_stay_text(shots):
    text = f"Checkout shows 500\n'{shots / 'with space.jpg'}' {shots / 'err.png'}\n/static/logo.png returns 404"
    clean, found = extract_images(text)
    assert [i.name for i in found] == ["with space.jpg", "err.png"]
    assert clean == "Checkout shows 500\n\n/static/logo.png returns 404"  # line breaks kept
    assert extract_images("GET /img/a.png returns 404") == ("GET /img/a.png returns 404", [])


def test_claude_sends_stream_json_with_image_blocks(shots, tmp_path):
    p = ClaudeCodeProvider(ClaudeConfig(executable=sys.executable))
    req = ProviderRequest(system_prompt="s", prompt="describe", images=[load_image(shots / "err.png")])
    argv = p.build_argv(req, tmp_path / "s.md")
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    msg = json.loads(p.stdin_for(req))
    content = msg["message"]["content"]
    assert msg["type"] == "user" and content[0] == {"type": "text", "text": "describe"}
    assert content[1]["source"]["media_type"] == "image/png" and base64.b64decode(content[1]["source"]["data"]) == PNG
    plain = ProviderRequest(system_prompt="s", prompt="describe")
    assert "--input-format" not in p.build_argv(plain, tmp_path / "s.md") and p.stdin_for(plain) == "describe"


def test_opencode_attaches_with_file_flag(shots):
    argv = OpenCodeProvider(OpenCodeConfig(executable=sys.executable)).build_argv([load_image(shots / "err.png")])
    assert argv[argv.index("-f") + 1] == str((shots / "err.png").resolve())


def test_cli_add_with_image_reaches_agent_and_is_archived(shots, tmp_path):
    record = tmp_path / "seen.json"
    custom_agent(record)
    r = runner.invoke(cli, ["add", "--image", str(shots / "err.png"), "Checkout returns 500"])
    assert r.exit_code == 0, r.output
    seen = json.loads(record.read_text())
    assert seen["images"][0]["media_type"] == "image/png" and seen["images"][0]["bytes"] > 0
    assert "1 screenshot(s) of the bug are attached" in seen["prompt"]
    from bugmind.service import BugMindService
    rec = BugMindService(load_config()).store.get("BUG-0001")
    assert rec.attachments == ["attachments/BUG-0001/1.png"]
    stored = BugMindService(load_config()).store.layout.root / rec.attachments[0]
    assert stored.read_bytes() == PNG  # evidence kept even if the original is deleted
    assert "Screenshot:" in runner.invoke(cli, ["show", "BUG-0001"]).output
    assert runner.invoke(cli, ["add", "--image", str(shots / "fake.png"), "x y"]).exit_code == 2


async def test_tui_drag_and_drop_screenshot_then_describe(shots, tmp_path):
    record = tmp_path / "seen.json"
    custom_agent(record)
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer
    app = BugMindApp(load_config())
    async with app.run_test(size=(150, 45)) as pilot:
        chat, composer = app.query_one(ChatView), app.query_one(Composer)
        composer.insert(f"'{shots / 'with space.jpg'}'")  # what GNOME Terminal pastes on drop
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert [i.name for i in chat.pending] == ["with space.jpg"] and app.query_one("#attach-bar").display
        composer.insert("/attach " + str(shots / "fake.png"))
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert len(chat.pending) == 1  # fake image refused with an error message
        composer.insert("Checkout page shows 500 after pressing Pay")
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.1)
            if not chat.busy and len(app.service.store) == 1:
                break
        assert json.loads(record.read_text())["images"][0]["name"] == "with space.jpg"
        assert chat.pending == [] and not app.query_one("#attach-bar").display
        assert app.service.store.get("BUG-0001").attachments == ["attachments/BUG-0001/1.jpg"]


def test_clipboard_image_text_and_copied_files(monkeypatch, shots):
    import bugmind.images as images
    clip = {}
    monkeypatch.setattr(images, "_clipboard_tools", lambda: (["list"], ["read"]))
    monkeypatch.setattr(images, "_run", lambda argv: clip.get(argv[-1]))
    clip.update({"list": b"TARGETS image/png", "image/png": PNG})
    imgs, text = images.read_clipboard()
    assert text is None and imgs[0].media_type == "image/png" and imgs[0].path.read_bytes() == PNG
    assert oct(imgs[0].path.stat().st_mode & 0o777) == "0o600"
    clip.clear()
    clip.update({"list": b"x-special/gnome-copied-files", "x-special/gnome-copied-files":
                 f"copy\nfile://{(shots / 'with space.jpg').as_posix().replace(' ', '%20')}".encode()})
    assert [i.name for i in images.read_clipboard()[0]] == ["with space.jpg"]
    clip.clear()
    clip.update({"list": b"UTF8_STRING", "UTF8_STRING": "سطر\nline".encode()})
    assert images.read_clipboard() == ([], "سطر\nline")
    clip.clear()
    clip.update({"list": b"image/png", "image/png": b"not an image"})
    with pytest.raises(ImageError):
        images.read_clipboard()


async def test_tui_ctrl_v_attaches_clipboard_image(monkeypatch, shots, tmp_path):
    custom_agent(tmp_path / "seen.json")
    import bugmind.tui.chat as chat_mod
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer
    monkeypatch.setattr(chat_mod, "read_clipboard", lambda: ([load_image(shots / "err.png")], None))
    app = BugMindApp(load_config())
    async with app.run_test(size=(150, 45)) as pilot:
        app.query_one(Composer).focus()
        await pilot.press("ctrl+v")
        await pilot.pause(0.3)
        assert [i.name for i in app.query_one(ChatView).pending] == ["err.png"]
        monkeypatch.setattr(chat_mod, "read_clipboard", lambda: ([], "pasted text"))
        await pilot.press("ctrl+v")
        await pilot.pause(0.3)
        assert app.query_one(Composer).text == "pasted text"
