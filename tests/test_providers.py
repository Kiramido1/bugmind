"""Provider adapters are exercised against real subprocesses (fake executables)."""

import asyncio
import json
import os
import stat
import sys
import tempfile
import textwrap

import pytest

from bugmind.config import ClaudeConfig, CustomConfig, OpenCodeConfig
from bugmind.providers import ErrorKind, ProviderError, ProviderRequest
from bugmind.providers.claude import ClaudeCodeProvider
from bugmind.providers.custom import CustomProvider
from bugmind.providers.opencode import OpenCodeProvider

from conftest import FAKE_AGENT

REQ = ProviderRequest(system_prompt="SYS untrusted rules", prompt="PROMPT body", timeout=15)


def script(tmp_path, name, body):
    """A fake agent CLI: an executable script on POSIX, a .cmd shim (like npm's) on Windows."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    if sys.platform == "win32":
        shim = tmp_path / f"{name}.cmd"
        shim.write_text(f'@"{sys.executable}" "{path}" %*\r\n', encoding="utf-8")
        return str(shim)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# ------------------------------------------------------------------ custom


def custom(mode="stdin_json", **env):
    return CustomProvider(CustomConfig(command=[sys.executable, str(FAKE_AGENT)], input_mode=mode, env=env))


async def test_custom_streams_and_returns_text():
    chunks = []
    res = await custom(FAKE_AGENT_MODE="bug").run(REQ, chunks.append)
    assert '"intent": "bug_report"' in res.text and chunks
    assert res.provider == "custom"


async def test_custom_envelope_and_error_envelope():
    res = await custom(FAKE_AGENT_MODE="envelope").run(REQ)
    assert json.loads(res.text)["intent"] == "bug_report"
    with pytest.raises(ProviderError) as e:
        await custom(FAKE_AGENT_MODE="error").run(REQ)
    assert "model overloaded" in str(e.value)


async def test_custom_failure_auth_timeout_and_missing(tmp_path):
    with pytest.raises(ProviderError) as e:
        await custom(FAKE_AGENT_MODE="fail").run(REQ)
    assert e.value.kind == ErrorKind.PROCESS and "boom" in str(e.value)
    with pytest.raises(ProviderError) as e:
        await custom(FAKE_AGENT_MODE="auth").run(REQ)
    assert e.value.kind == ErrorKind.AUTH
    with pytest.raises(ProviderError) as e:
        await custom(FAKE_AGENT_MODE="sleep").run(ProviderRequest("s", "p", timeout=0.5))
    assert e.value.kind == ErrorKind.TIMEOUT
    with pytest.raises(ProviderError) as e:
        await CustomProvider(CustomConfig(command=["definitely-not-a-binary-xyz"])).run(REQ)
    assert e.value.kind == ErrorKind.MISSING
    with pytest.raises(ProviderError) as e:
        await CustomProvider(CustomConfig(command=[])).run(REQ)
    assert e.value.kind == ErrorKind.MISSING


async def test_custom_file_mode_and_no_shell_interpolation(tmp_path):
    marker = tmp_path / "pwned"
    exe = script(tmp_path, "agent", """
        import json, sys
        req = json.load(open(sys.argv[1]))
        print(json.dumps({"bugmind_protocol": 1, "text": "argv=" + json.dumps(sys.argv[1:]) + " prompt=" + req["prompt"]}))
    """)
    p = CustomProvider(CustomConfig(command=[exe, "{prompt_file}", "$(touch " + str(marker) + ")"], input_mode="file"))
    res = await p.run(ProviderRequest("s", "hello; rm -rf ~", timeout=10))
    assert "prompt=hello; rm -rf ~" in res.text
    assert "$(touch" in res.text and not marker.exists()  # literal argument, never a shell
    assert not any(n.startswith("bugmind-request-") for n in os.listdir(tempfile.gettempdir())
                   if n.endswith(".json") and (tmp_path / n).exists())


async def test_custom_check_reports_missing_command():
    status = await CustomProvider(CustomConfig()).check()
    assert not status.available and "No command" in status.detail


# ------------------------------------------------------------------ claude (fake executable, real output format)


FAKE_CLAUDE = """
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
if "--version" in args:
    print("2.1.325 (Claude Code)"); sys.exit(0)
assert args[args.index("--tools") + 1] == ""
assert "--strict-mcp-config" in args and "-p" in args
sysp = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
mode = __import__("os").environ.get("FAKE_CLAUDE_MODE", "ok")
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "system", "subtype": "init"})
if mode == "auth":
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}); sys.exit(1)
for part in ["{\\"intent\\": ", "\\"question\\", \\"reply\\": \\"", sysp[:3], "\\"}"]:
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
          "delta": {"type": "text_delta", "text": part}}})
text = "{\\"intent\\": \\"question\\", \\"reply\\": \\"" + sysp[:3] + "\\"}"
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0.01})
"""


async def test_claude_adapter_streams_and_parses(tmp_path):
    exe = script(tmp_path, "claude", FAKE_CLAUDE)
    p = ClaudeCodeProvider(ClaudeConfig(executable=exe, model="sonnet"))
    argv = p.build_argv(REQ, tmp_path / "sys.md")
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "low"
    assert "--effort" not in ClaudeCodeProvider(ClaudeConfig(executable=exe, effort=None)).build_argv(REQ, tmp_path / "s")
    chunks = []
    res = await p.run(REQ, chunks.append)
    assert json.loads(res.text) == {"intent": "question", "reply": "SYS"}
    assert "".join(chunks) == res.text and res.cost_usd == 0.01
    status = await p.check()
    assert status.available and status.version.startswith("2.1")


async def test_claude_adapter_auth_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    p = ClaudeCodeProvider(ClaudeConfig(executable=script(tmp_path, "claude", FAKE_CLAUDE)))
    with pytest.raises(ProviderError) as e:
        await p.run(REQ)
    assert e.value.kind == ErrorKind.AUTH


async def test_claude_missing_executable():
    p = ClaudeCodeProvider(ClaudeConfig(executable="/nonexistent/claude"))
    with pytest.raises(ProviderError) as e:
        await p.run(REQ)
    assert e.value.kind == ErrorKind.MISSING
    assert not (await p.check()).available


# ------------------------------------------------------------------ opencode


FAKE_OPENCODE = """
import json, os, sys
args = sys.argv[1:]
if "--version" in args:
    print("1.19.5"); sys.exit(0)
assert args[:3] == ["run", "--format", "json"]
assert json.loads(os.environ["OPENCODE_PERMISSION"])["bash"] == "deny"
msg = sys.stdin.read()
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "step_start", "part": {"type": "step-start"}})
if os.environ.get("FAKE_OC_MODE") == "error":
    emit({"type": "error", "error": {"name": "UnknownError", "data": {"message": "Unexpected server error"}}}); sys.exit(0)
ok = msg.startswith("SYS") and "PROMPT body" in msg
emit({"type": "text", "part": {"type": "text", "text": json.dumps({"intent": "other", "reply": str(ok)})}})
emit({"type": "step_finish", "part": {"type": "step-finish", "reason": "stop", "cost": 0.002}})
"""


async def test_opencode_adapter_parses_events_and_denies_tools(tmp_path):
    p = OpenCodeProvider(OpenCodeConfig(executable=script(tmp_path, "opencode", FAKE_OPENCODE), agent="plan"))
    assert p.build_argv()[-2:] == ["--agent", "plan"]
    chunks = []
    res = await p.run(REQ, chunks.append)
    assert json.loads(res.text) == {"intent": "other", "reply": "True"}  # system prompt was prepended
    assert chunks and res.cost_usd == pytest.approx(0.002)


async def test_opencode_error_event_with_exit_zero_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OC_MODE", "error")
    p = OpenCodeProvider(OpenCodeConfig(executable=script(tmp_path, "opencode", FAKE_OPENCODE)))
    with pytest.raises(ProviderError) as e:
        await p.run(REQ)
    assert "Unexpected server error" in str(e.value)


# ------------------------------------------------------------------ process lifecycle (QA regressions)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) would *terminate* the process on Windows
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombies count as dead
        return open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except OSError:
        return False


async def test_timeout_kills_background_child_even_if_leader_exited(tmp_path):
    from bugmind.providers.process import run_process
    pidfile = tmp_path / "pid"
    with pytest.raises(ProviderError) as e:
        # the leader starts a sleeper that keeps our stdout pipe open, records its pid and exits at once
        await run_process([sys.executable, "-c", textwrap.dedent(f"""
            import subprocess, sys, pathlib
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=sys.stdout)
            pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))
        """)], provider="t", stdin_data=None, timeout=3)
    assert e.value.kind.value == "timeout"
    await asyncio.sleep(0.2)
    assert not _alive(int(pidfile.read_text(encoding="utf-8")))


async def test_parser_failure_terminates_agent(tmp_path):
    from bugmind.providers.process import run_process
    pidfile = tmp_path / "pid"

    def boom(line: str) -> None:
        raise RuntimeError("bad event")

    with pytest.raises(ProviderError) as e:
        await run_process([sys.executable, "-c", textwrap.dedent(f"""
            import os, pathlib, time
            pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))
            print("x", flush=True)
            time.sleep(60)
        """)], provider="t", stdin_data=None, timeout=30, on_line=boom)
    assert e.value.kind.value == "protocol"
    await asyncio.sleep(0.2)
    assert not _alive(int(pidfile.read_text(encoding="utf-8")))


async def test_opencode_string_error_event_is_reported_not_crashing(tmp_path):
    exe = script(tmp_path, "opencode", 'import json\nprint(json.dumps({"type": "error", "error": "rate limited"}))\n')
    with pytest.raises(ProviderError) as e:
        await OpenCodeProvider(OpenCodeConfig(executable=exe)).run(REQ)
    assert "rate limited" in str(e.value)


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe batch-file argument parsing")
async def test_batch_file_agent_refuses_cmd_metacharacters(tmp_path):
    from bugmind.providers.process import run_process
    exe = script(tmp_path, "agent", "print('ran')")
    marker = tmp_path / "pwned"
    with pytest.raises(ProviderError) as e:
        await run_process([exe, f'x" & type nul > "{marker}'], provider="t", stdin_data=None, timeout=10)
    assert "cmd.exe" in str(e.value) and not marker.exists()
    out = await run_process([exe, "plain arg"], provider="t", stdin_data=None, timeout=10)
    assert out.stdout.strip() == "ran"


async def test_agent_tree_is_gone_after_a_normal_exit(tmp_path):
    """A background helper the agent leaves behind is killed once the agent's answer is complete."""
    from bugmind.providers.process import run_process
    pidfile = tmp_path / "pid"
    await run_process([sys.executable, "-c", textwrap.dedent(f"""
        import subprocess, sys, pathlib
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))
        print("done")
    """)], provider="t", stdin_data=None, timeout=30)
    await asyncio.sleep(0.3)
    assert not _alive(int(pidfile.read_text(encoding="utf-8")))
