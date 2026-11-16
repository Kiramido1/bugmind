"""Custom agent adapter. See docs/PROVIDERS.md for the full I/O contract.

Input (input_mode):
  stdin_json (default)  stdin receives one JSON object:
      {"bugmind_protocol": 1, "system": "<instructions>", "prompt": "<task>", "response_format": "json"}
  stdin_text            stdin receives "<system>\\n\\n---\\n\\n<prompt>"
  file                  the JSON object above is written to a private temp file; every
                        argument containing "{prompt_file}" has it replaced by that path.

Output: the agent writes its answer to stdout. Either the raw answer text, or a
JSON envelope {"bugmind_protocol": 1, "text": "<answer>"} / {"error": "<message>"}.
Exit code 0 means success. stdout is streamed to the UI line by line.

The command is an argv list executed without a shell; nothing from the bug text
is ever interpolated into it.
"""

from __future__ import annotations

import json
import os
import tempfile

from bugmind.config import CustomConfig
from bugmind.providers.base import (
    ChunkCallback, ErrorKind, Provider, ProviderError, ProviderRequest, ProviderResult, ProviderStatus,
    agent_workdir, looks_like_auth_error,
)
from bugmind.providers.process import resolve_executable, run_process

PROTOCOL_VERSION = 1


class CustomProvider(Provider):
    name = "custom"
    supports_streaming = True

    def __init__(self, cfg: CustomConfig) -> None:
        self.cfg = cfg
        self.label = cfg.name or "Custom Agent"

    async def check(self) -> ProviderStatus:
        if not self.cfg.command:
            return ProviderStatus(self.name, self.label, False, None,
                                  "No command configured (provider.custom.command)")
        try:
            path = resolve_executable(self.cfg.command[0], self.name)
        except ProviderError as exc:
            return ProviderStatus(self.name, self.label, False, None, str(exc.args[0]))
        return ProviderStatus(self.name, self.label, True, None, path)

    async def run(self, request: ProviderRequest, on_chunk: ChunkCallback | None = None) -> ProviderResult:
        if not self.cfg.command:
            raise ProviderError(ErrorKind.MISSING, "No command configured (provider.custom.command)", self.name)
        exe = resolve_executable(self.cfg.command[0], self.name)
        if request.images and self.cfg.input_mode == "stdin_text":
            raise ProviderError(ErrorKind.PROCESS, "Screenshots need input_mode stdin_json or file "
                                "(stdin_text can only carry text).", self.name)
        payload = json.dumps({
            "bugmind_protocol": PROTOCOL_VERSION,
            "system": request.system_prompt,
            "prompt": request.prompt,
            "response_format": "json",
            **({"images": [{"name": i.name, "media_type": i.media_type, "data_base64": i.read_base64()}
                           for i in request.images]} if request.images else {}),
        }, ensure_ascii=False)

        tmp_path: str | None = None
        stdin_data: str | None
        args = list(self.cfg.command[1:])
        if self.cfg.input_mode == "file":
            fd, tmp_path = tempfile.mkstemp(prefix="bugmind-request-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            args = [a.replace("{prompt_file}", tmp_path) for a in args]
            stdin_data = None
        elif self.cfg.input_mode == "stdin_text":
            stdin_data = f"{request.system_prompt}\n\n---\n\n{request.prompt}"
        else:
            stdin_data = payload

        def on_line(line: str) -> None:
            if on_chunk:
                on_chunk(line + "\n")

        try:
            out = await run_process(
                [exe, *args], provider=self.name, stdin_data=stdin_data, timeout=request.timeout,
                on_line=on_line, env=dict(self.cfg.env), cwd=agent_workdir(),
            )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if out.returncode != 0:
            detail = (out.stderr.strip() or out.stdout.strip())[-500:]
            kind = ErrorKind.AUTH if looks_like_auth_error(detail) else ErrorKind.PROCESS
            raise ProviderError(kind, f"exit code {out.returncode}: {detail or 'no output'}", self.name)
        text = out.stdout.strip()
        try:
            env = json.loads(text)
        except json.JSONDecodeError:
            env = None
        if isinstance(env, dict) and env.get("bugmind_protocol") is not None or (
            isinstance(env, dict) and set(env) <= {"error", "text", "bugmind_protocol"} and env
        ):
            if env.get("error"):
                raise ProviderError(ErrorKind.PROCESS, str(env["error"])[:500], self.name)
            text = str(env.get("text", ""))
        if not text:
            raise ProviderError(ErrorKind.PROTOCOL, "agent produced no output", self.name)
        return ProviderResult(text=text, provider=self.name, duration_s=out.duration_s)
