"""OpenCode adapter.

Verified against OpenCode 1.19.x (`opencode run --help`):
  opencode run --format json [--model provider/model] [--agent name]
The message is read from stdin (OpenCode appends piped stdin to the message).

`--format json` emits newline-delimited events:
  {"type":"step_start", ...}
  {"type":"text","part":{"type":"text","text":"..."}}
  {"type":"step_finish","part":{"reason":"stop","cost":0, ...}}
  {"type":"error","error":{"name":"...","data":{"message":"..."}}}
OpenCode exits 0 even on errors, so "error" events are authoritative.

OpenCode has no system-prompt flag, so BugMind's instructions are prepended to the
message. Tools are denied through the OPENCODE_PERMISSION environment variable,
and the process runs in an empty BugMind-owned directory.
"""

from __future__ import annotations

import json

from bugmind.config import OpenCodeConfig
from bugmind.providers.base import (
    as_dict as _dict,
    ChunkCallback, ErrorKind, Provider, ProviderError, ProviderRequest, ProviderResult, ProviderStatus,
    agent_workdir, looks_like_auth_error,
)
from bugmind.providers.process import probe_version, resolve_executable, run_process

DENY_ALL_TOOLS = json.dumps({"edit": "deny", "bash": "deny", "webfetch": "deny", "external_directory": "deny"})


class OpenCodeProvider(Provider):
    name = "opencode"
    label = "OpenCode"
    supports_streaming = True  # text parts arrive as they complete

    def __init__(self, cfg: OpenCodeConfig) -> None:
        self.cfg = cfg

    async def check(self) -> ProviderStatus:
        try:
            version = await probe_version(self.cfg.executable, self.name)
            return ProviderStatus(self.name, self.label, True, version, resolve_executable(self.cfg.executable, self.name))
        except ProviderError as exc:
            return ProviderStatus(self.name, self.label, False, None, str(exc.args[0]))

    def build_argv(self, images: list | None = None) -> list[str]:
        exe = resolve_executable(self.cfg.executable, self.name)
        argv = [exe, "run", "--format", "json"]
        for img in images or []:  # `opencode run -f <file>` attaches it (verified with OpenCode 1.19.x)
            argv += ["-f", str(img.path)]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        if self.cfg.agent:
            argv += ["--agent", self.cfg.agent]
        argv += list(self.cfg.extra_args)
        return argv

    @staticmethod
    def compose_message(request: ProviderRequest) -> str:
        return f"{request.system_prompt}\n\n---\n\n{request.prompt}"

    async def run(self, request: ProviderRequest, on_chunk: ChunkCallback | None = None) -> ProviderResult:
        argv = self.build_argv(request.images)
        texts: list[str] = []
        errors: list[str] = []
        cost: float | None = None

        def on_line(line: str) -> None:
            nonlocal cost
            if not line.strip():
                return
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                return
            if not isinstance(evt, dict):
                return
            etype = evt.get("type")
            part = _dict(evt.get("part"))
            if etype == "text":
                text = str(part.get("text", ""))
                if text:
                    texts.append(text)
                    if on_chunk:
                        on_chunk(text)
            elif etype == "error":
                raw = evt.get("error")
                err = _dict(raw)
                errors.append(str(_dict(err.get("data")).get("message") or err.get("name")
                                  or (raw if isinstance(raw, str) else "") or "unknown error"))
            elif etype == "step_finish" and isinstance(part.get("cost"), (int, float)):
                cost = (cost or 0.0) + float(part["cost"])

        out = await run_process(
            argv, provider=self.name, stdin_data=self.compose_message(request), timeout=request.timeout,
            on_line=on_line, env={"OPENCODE_PERMISSION": DENY_ALL_TOOLS}, cwd=agent_workdir(),
        )
        if errors:
            msg = "; ".join(errors)[:500]
            raise ProviderError(ErrorKind.AUTH if looks_like_auth_error(msg) else ErrorKind.PROCESS, msg, self.name)
        if out.returncode != 0:
            detail = out.stderr.strip()[-500:]
            kind = ErrorKind.AUTH if looks_like_auth_error(detail) else ErrorKind.PROCESS
            raise ProviderError(kind, f"exit code {out.returncode}: {detail or 'no output'}", self.name)
        text = "\n".join(texts).strip()
        if not text:
            raise ProviderError(ErrorKind.PROTOCOL, f"no text events in output. {out.stderr.strip()[-300:]}", self.name)
        return ProviderResult(text=text, provider=self.name, duration_s=out.duration_s, cost_usd=cost)
