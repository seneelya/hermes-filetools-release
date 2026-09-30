"""Unit tests for file_ops/vision_analyze_core.py (Plan05).

Two halves, and the first one is the actual bug fix:

  * `is_path_source` / `path_to_data_url` — which sources we take over, and
    that an unreadable path produces a PATH error rather than upstream's
    "sandbox returned non-image data" lie. That lie is what sent the model
    into hand-rolling base64 data URLs, so it is the regression to guard.
  * `render_vision_result` / `vision_analyze_tool_new` — the reformatting, and
    the one thing that must NOT be reformatted: a dict return. On the native
    fast path `_handle_vision_analyze` answers with a `_multimodal` envelope
    dict that the agent loop unwraps; rendering it would break vision
    entirely.

All pure - no Hermes, no HERMES_SRC. The file backend and their handler are
both injected/stubbed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import types
from types import SimpleNamespace as NS

import pytest

from conftest import split_op_line   # our tools lead every answer with `op: <id>`

sys.path.insert(0, "file_ops")
import vision_analyze_core as vac  # type: ignore


class FakeBackend:
    """Stands in for file_ops/file_backend.py's FileBackend: resolve() applies
    the container-aware resolution, ops.read_file_bytes() reads in that same
    environment."""

    def __init__(self, files=None, resolve_error=None, read_raises=None):
        self._files = files or {}
        self._resolve_error = resolve_error
        self._read_raises = read_raises
        self.ops = self

    def resolve(self, path):
        if self._resolve_error:
            raise RuntimeError(self._resolve_error)
        return path

    def read_file_bytes(self, path, max_bytes=None):
        if self._read_raises:
            raise RuntimeError(self._read_raises)
        if path not in self._files:
            return NS(error=f"File not found: {path}", base64_content=None,
                      file_size=0)
        data = self._files[path]
        if max_bytes is not None and len(data) > max_bytes:
            return NS(error=f"File is too large ({len(data):,} bytes, limit "
                             f"is {max_bytes:,})",
                      base64_content=None, file_size=len(data))
        return NS(error=None,
                  base64_content=base64.b64encode(data).decode("ascii"),
                  file_size=len(data))


# ── which sources are ours ─────────────────────────────────────────────────

@pytest.mark.parametrize("src", [
    "/workspace/temp/screen_copy.jpg",
    "pic.png",
    "C:\\Users\\me\\shot.png",
    "file:///workspace/temp/x.jpg",
    "./relative/x.webp",
])
def test_path_sources_are_taken_over(src):
    assert vac.is_path_source(src) is True


@pytest.mark.parametrize("src", [
    "data:image/png;base64,AAAA",
    "http://example.com/x.png",
    "https://example.com/x.png",
    "HTTPS://EXAMPLE.COM/x.png",
])
def test_working_sources_are_left_to_upstream(src):
    """Nothing is broken about these branches and there is nothing for us to
    fix - touching them would only add a way to break them."""
    assert vac.is_path_source(src) is False


def test_unknown_scheme_is_left_to_upstream():
    """So the model sees upstream's own UnsupportedScheme message instead of a
    confusing file-not-found from us."""
    assert vac.is_path_source("s3://bucket/x.png") is False
    assert vac.is_path_source("ftp://host/x.png") is False


def test_empty_source_is_not_ours():
    assert vac.is_path_source("") is False
    assert vac.is_path_source("   ") is False
    assert vac.is_path_source(None) is False


# ── path -> data URL ───────────────────────────────────────────────────────

def test_good_path_becomes_a_data_url_with_the_real_bytes():
    png = b"\x89PNG\r\n\x1a\nfake"
    backend = FakeBackend({"/workspace/temp/x.png": png})
    url, error = vac.path_to_data_url("/workspace/temp/x.png", backend=backend)
    assert error is None
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png


def test_file_uri_prefix_is_stripped_before_reading():
    backend = FakeBackend({"/workspace/x.jpg": b"\xff\xd8\xffjpeg"})
    url, error = vac.path_to_data_url("file:///workspace/x.jpg", backend=backend)
    assert error is None
    assert url.startswith("data:image/jpeg;base64,")


def test_unknown_extension_declares_octet_stream():
    """A declaration, not a claim: upstream's _finalize verifies the real type
    from the magic bytes, and that verification is what keeps "not an image"
    an honest, separate error."""
    backend = FakeBackend({"/workspace/x": b"\x89PNG\r\n\x1a\n"})
    url, error = vac.path_to_data_url("/workspace/x", backend=backend)
    assert error is None
    assert url.startswith("data:application/octet-stream;base64,")


def test_missing_path_is_a_PATH_error_not_a_content_type_error():
    """The whole point of Plan05's error half. Upstream raised
    NotAnImage("sandbox returned non-image data") for a file that simply was
    not there, and that message is what drove the model into base64
    gymnastics."""
    backend = FakeBackend({})
    url, error = vac.path_to_data_url("/workspace/nope.png", backend=backend)
    assert url is None
    assert "could not read image file" in error
    assert "/workspace/nope.png" in error
    assert "File not found" in error
    assert "not an image-format problem" in error
    assert "base64" in error  # tells the model explicitly not to do that


def test_oversize_file_reports_the_limit():
    backend = FakeBackend({"/workspace/big.png": b"x" * 100})
    url, error = vac.path_to_data_url("/workspace/big.png", max_bytes=10,
                                       backend=backend)
    assert url is None
    assert "too large" in error


def test_resolve_failure_is_reported_as_a_path_problem():
    backend = FakeBackend(resolve_error="no such task")
    url, error = vac.path_to_data_url("/x.png", backend=backend)
    assert url is None
    assert "could not resolve image path" in error


def test_read_exception_is_reported_not_raised():
    backend = FakeBackend(read_raises="backend exploded")
    url, error = vac.path_to_data_url("/x.png", backend=backend)
    assert url is None
    assert "could not read image file" in error
    assert "backend exploded" in error


def test_empty_content_is_reported_rather_than_becoming_an_empty_data_url():
    class _Empty(FakeBackend):
        def read_file_bytes(self, path, max_bytes=None):
            return NS(error=None, base64_content="", file_size=0)

    url, error = vac.path_to_data_url("/x.png", backend=_Empty())
    assert url is None
    assert "no content back" in error


# ── rendering ──────────────────────────────────────────────────────────────

def test_success_renders_analysis_as_the_body():
    raw = json.dumps({"success": True, "analysis": "A cat on a keyboard."})
    head, sep, body = vac.render_vision_result(raw).partition("\n○○○\n")
    assert sep
    assert head == "Success: True"
    assert body == "A cat on a keyboard."


def test_scale_note_reaches_the_header():
    raw = json.dumps({"success": True, "analysis": "text",
                       "scale_note": "downscaled 2x"})
    result = vac.render_vision_result(raw)
    assert "Scale note: downscaled 2x" in result


def test_failure_renders_error_and_keeps_whatever_analysis_survived():
    raw = json.dumps({"success": False, "error": "provider timeout",
                       "analysis": "partial"})
    result = vac.render_vision_result(raw)
    assert result.startswith("Success: False\nError: provider timeout\n○○○\n")
    assert result.endswith("partial")


def test_tool_error_shape_renders_as_lines_not_json():
    raw = json.dumps({"error": "image_url is required", "success": False})
    result = vac.render_vision_result(raw)
    assert "{" not in result
    assert "Error: image_url is required" in result


# ── the live call ──────────────────────────────────────────────────────────

def _stub_handler(monkeypatch, fn):
    fake_mod = types.ModuleType("tools.vision_tools")
    fake_mod._handle_vision_analyze = fn
    monkeypatch.setitem(sys.modules, "tools.vision_tools", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))


def test_dict_return_passes_through_untouched(monkeypatch):
    """The native-vision fast path answers with a `_multimodal` envelope dict
    that the agent loop unwraps. Rendering it would destroy vision entirely,
    so a non-string return must come back byte-identical."""
    envelope = {"_multimodal": True, "content": [{"type": "image"}]}

    async def _fake(args, **kw):
        return envelope

    _stub_handler(monkeypatch, _fake)
    got = asyncio.run(vac.vision_analyze_tool_new(
        {"image_url": "data:image/png;base64,AA", "question": "?"}))
    assert got is envelope


def test_string_return_is_rendered(monkeypatch):
    async def _fake(args, **kw):
        return json.dumps({"success": True, "analysis": "described"})

    _stub_handler(monkeypatch, _fake)
    got = asyncio.run(vac.vision_analyze_tool_new(
        {"image_url": "https://x/y.png", "question": "?"}))
    _op, rest = split_op_line(got)   # the address leads the answer (Plan09)
    assert rest == "Success: True\n○○○\ndescribed"


def test_a_path_reaches_their_handler_as_a_data_url(monkeypatch):
    """The fix, end to end: upstream never sees the path, so its Windows
    pathlib cannot mangle it."""
    seen = {}

    async def _fake(args, **kw):
        seen.update(args)
        return json.dumps({"success": True, "analysis": "ok"})

    _stub_handler(monkeypatch, _fake)
    monkeypatch.setattr(vac, "path_to_data_url",
                        lambda src, task_id="default", **kw: ("data:image/png;base64,AA", None))

    asyncio.run(vac.vision_analyze_tool_new(
        {"image_url": "/workspace/temp/screen_copy.jpg", "question": "what?"}))

    assert seen["image_url"] == "data:image/png;base64,AA"
    assert seen["question"] == "what?"
    assert "\\workspace" not in seen["image_url"]


def test_unreadable_path_never_reaches_their_handler(monkeypatch):
    """Their error message for this case is the misleading one. Ours must win,
    which means not calling them at all."""
    called = []

    async def _fake(args, **kw):
        called.append(args)
        return "{}"

    _stub_handler(monkeypatch, _fake)
    monkeypatch.setattr(vac, "path_to_data_url",
                        lambda src, task_id="default", **kw: (None, "ERROR: vision_analyze: nope"))

    got = asyncio.run(vac.vision_analyze_tool_new(
        {"image_url": "/workspace/nope.png", "question": "?"}))
    assert got == "ERROR: vision_analyze: nope"
    assert called == []


def test_original_args_dict_is_not_mutated(monkeypatch):
    async def _fake(args, **kw):
        return json.dumps({"success": True, "analysis": "ok"})

    _stub_handler(monkeypatch, _fake)
    monkeypatch.setattr(vac, "path_to_data_url",
                        lambda src, task_id="default", **kw: ("data:image/png;base64,AA", None))

    original = {"image_url": "/workspace/x.png", "question": "?"}
    asyncio.run(vac.vision_analyze_tool_new(original))
    assert original["image_url"] == "/workspace/x.png"


def test_is_ours_recognises_our_vision_text():
    """CROSS-HALF test, same purpose as the execute_code one: /ops matches
    literal fragments of OUR text. Also pins the one row we must NOT claim -
    on the native-vision fast path their handler returns a `_multimodal` dict
    that our wrapper passes through untouched, so that answer is not our text
    and the panel has no business presenting it as ours.
    """
    import sys
    sys.path.insert(0, "dashboard")
    import plugin_api  # type: ignore

    ok = vac.render_vision_result(json.dumps({
        "success": True, "analysis": "a cat on a keyboard"}))
    failed = vac.render_vision_result(json.dumps({
        "success": False, "error": "cannot read that path inside the sandbox"}))
    builtin_json = json.dumps({"success": True, "analysis": "a cat"})

    def row(content):
        return {"tool_name": "vision_analyze", "content": content}

    assert plugin_api._is_ours(row(ok)) is True
    assert plugin_api._is_ours(row(failed)) is True
    assert plugin_api._is_ours(row(builtin_json)) is False
