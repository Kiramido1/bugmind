"""Claude Code adapter.

Verified against Claude Code 2.1.x (`claude --help`):
  claude -p --output-format stream-json --verbose --include-partial-messages
         --tools "" --no-session-persistence --strict-mcp-config
         --disable-slash-commands --system-prompt-file <file>
The task prompt is sent on stdin. With screenshots, `--input-format stream-json` is added and stdin
carries one {"type":"user"} message whose content holds the text plus base64 image blocks. The system prompt goes through a private temp file: it embeds
the whole knowledge base and would exceed Linux's 128 KiB per-argument limit as an argv entry.

Output is newline-delimited JSON:
  {"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
  {"type":"result","subtype":"success","is_error":false,"result":"...","total_cost_usd":0.01}

`--tools ""` disables every built-in tool, so the agent cannot read or modify files
or run commands; `--strict-mcp-config` (with no --mcp-config) loads no MCP servers.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from bugmind.config import ClaudeConfig
from bugmind.providers.base import (
    as_dict as _dict,
    ChunkCallback, ErrorKind, Provider, ProviderError, ProviderRequest, ProviderResult, ProviderStatus,
    agent_workdir, looks_like_auth_error,
)
from bugmind.providers.process import probe_version, resolve_executable, run_process


class ClaudeCodeProvider(Provider):
    name = "claude"
    label = "Claude Code"
    supports_streaming = True

    def __init__(self, cfg: ClaudeConfig) -> None:
        self.cfg = cfg

    async def check(self) -> ProviderStatus:
        try:
            version = await probe_version(self.cfg.executable, self.name)
            return ProviderStatus(self.name, self.label, True, version, resolve_executable(self.cfg.executable, self.name))
        except ProviderError as exc:
            return ProviderStatus(self.name, self.label, False, None, str(exc.args[0]))

    def build_argv(self, request: ProviderRequest, system_prompt_file: Path) -> list[str]:
        exe = resolve_executable(self.cfg.executable, self.name)
        argv = [
            exe, "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--system-prompt-file", str(system_prompt_file),
        ]
        if request.images:  # images need a structured user message (verified with Claude Code 2.1.x)
            argv += ["--input-format", "stream-json"]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        if self.cfg.effort:
            argv += ["--effort", self.cfg.effort]
        argv += list(self.cfg.extra_args)
        return argv

    @staticmethod
    def stdin_for(request: ProviderRequest) -> str:
        """Plain prompt, or one stream-json user message with the screenshots as base64 image blocks."""
        if not request.images:
            return request.prompt
        content: list[dict] = [{"type": "text", "text": request.prompt}]
        content += [{"type": "image", "source": {"type": "base64", "media_type": img.media_type,
                                                 "data": img.read_base64()}} for img in request.images]
        return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"

    async def run(self, request: ProviderRequest, on_chunk: ChunkCallback | None = None) -> ProviderResult:
        fd, name = tempfile.mkstemp(prefix="bugmind-system-", suffix=".md")  # mode 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(request.system_prompt)
            return await self._run(request, Path(name), on_chunk)
        finally:
            Path(name).unlink(missing_ok=True)

    async def _run(self, request: ProviderRequest, system_prompt_file: Path,
                   on_chunk: ChunkCallback | None) -> ProviderResult:
        argv = self.build_argv(request, system_prompt_file)
        deltas: list[str] = []
        assistant_text: list[str] = []
        result: dict | None = None

        def on_line(line: str) -> None:
            nonlocal result
            if not line.strip():
                return
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                return
            if not isinstance(evt, dict):
                return
            etype = evt.get("type")
            if etype == "stream_event":
                inner = _dict(evt.get("event"))
                delta = _dict(inner.get("delta"))
                if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    text = str(delta.get("text", ""))
                    deltas.append(text)
                    if on_chunk and text:
                        on_chunk(text)
            elif etype == "assistant":
                content = _dict(evt.get("message")).get("content")
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        assistant_text.append(str(block.get("text", "")))
            elif etype == "result":
                result = evt

        out = await run_process(
            argv, provider=self.name, stdin_data=self.stdin_for(request), timeout=request.timeout,
            on_line=on_line, cwd=agent_workdir(),
        )
        if result is not None:
            text = str(result.get("result") or "")
            if result.get("is_error") or result.get("subtype") not in (None, "success"):
                msg = text or str(result.get("subtype")) or "unknown error"
                kind = ErrorKind.AUTH if looks_like_auth_error(msg) else ErrorKind.PROCESS
                raise ProviderError(kind, msg[:500], self.name)
            return ProviderResult(
                text=text or "".join(assistant_text) or "".join(deltas),
                provider=self.name,
                duration_s=out.duration_s,
                cost_usd=result.get("total_cost_usd") if isinstance(result.get("total_cost_usd"), (int, float)) else None,
            )
        detail = (out.stderr.strip() or out.stdout.strip())[-500:]
        if out.returncode != 0:
            kind = ErrorKind.AUTH if looks_like_auth_error(detail) else ErrorKind.PROCESS
            raise ProviderError(kind, f"exit code {out.returncode}: {detail or 'no output'}", self.name)
        text = "".join(assistant_text) or "".join(deltas)
        if not text:
            raise ProviderError(ErrorKind.PROTOCOL, f"no result event in output. {detail}", self.name)
        return ProviderResult(text=text, provider=self.name, duration_s=out.duration_s)
