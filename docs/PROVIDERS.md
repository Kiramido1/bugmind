# AI providers

All providers implement one async contract (`bugmind/providers/base.py`):

- `check()` — is the executable present, which version (never calls a model);
- `run(request, on_chunk)` — send `system_prompt` + `prompt`, stream text chunks, return the final
  text; raise `ProviderError(kind=missing|auth|timeout|process|protocol)` on failure.

Every agent process is started **without a shell** (argv list), in its own process group (killed
entirely on timeout or `Esc`), in an empty BugMind-owned working directory
(`~/.cache/bugmind/agent-workdir`) so it cannot pick up project files or instructions. Bug text is
passed on stdin, never interpolated into a command line.

The AI only analyses. Python validates the JSON it returns (Pydantic), assigns IDs and patterns,
deduplicates, persists, and writes the knowledge base.

## Claude Code

Verified against Claude Code 2.1.x:

```
claude -p --output-format stream-json --verbose --include-partial-messages \
       --tools "" --no-session-persistence --strict-mcp-config --disable-slash-commands \
       --system-prompt-file <private temp file with BugMind instructions> [--model M] [extra_args…]     < prompt on stdin
```

- `--tools ""` disables every built-in tool; `--strict-mcp-config` loads no MCP servers.
- Streaming comes from `stream_event` / `text_delta` events; the final `result` event provides
  the text, `is_error` and cost. "Not logged in" results are reported as auth errors.
- Config: `provider.claude.executable`, `.model`, `.extra_args`.

## OpenCode

Verified against OpenCode 1.19.x:

```
OPENCODE_PERMISSION='{"edit":"deny","bash":"deny","webfetch":"deny",…}' \
opencode run --format json [--model provider/model] [--agent A] [extra_args…]   < message on stdin
```

- OpenCode has no system-prompt flag, so instructions are prepended to the message.
- `text` events carry the answer; `error` events are treated as failures (OpenCode exits 0 even on
  errors). Costs from `step_finish` are summed.
- Config: `provider.opencode.executable`, `.model`, `.agent`, `.extra_args`.

## Custom agent

```yaml
provider:
  default: custom
  custom:
    name: My Agent
    command: ["/usr/local/bin/my-agent", "--json"]   # argv, no shell
    input_mode: stdin_json                           # stdin_json | stdin_text | file
    env: {}                                          # extra env vars (keep secrets in your own env instead)
```

### Input

| `input_mode` | What the agent receives |
|---|---|
| `stdin_json` (default) | stdin: `{"bugmind_protocol": 1, "system": "...", "prompt": "...", "response_format": "json"}` |
| `stdin_text` | stdin: `<system>\n\n---\n\n<prompt>` |
| `file` | the JSON object above in a private temp file; each argument containing `{prompt_file}` gets the path |

With screenshots, the JSON object (`stdin_json` / `file`) also has
`"images": [{"name": "shot.png", "media_type": "image/png", "data_base64": "..."}]`; `stdin_text`
cannot carry images and is refused with a clear error. Claude Code receives them as image blocks in a
`--input-format stream-json` user message; OpenCode via `opencode run -f <file>`. The agent never reads
files itself: BugMind validates each image and passes the content.

### Output

Write the answer to **stdout** and exit `0`. Either:

- the raw answer text (the JSON object described in the system prompt, optionally inside a
  ```` ```json ```` fence), or
- an envelope `{"bugmind_protocol": 1, "text": "<answer>"}`, or
- `{"error": "message"}` to report a failure.

A non-zero exit code is a failure; stderr is shown to the user (truncated). stdout lines are
streamed to the TUI as they arrive.

### Minimal example (Python)

```python
#!/usr/bin/env python3
import json, sys
req = json.load(sys.stdin)
answer = call_my_model(system=req["system"], user=req["prompt"])  # your code
print(json.dumps({"bugmind_protocol": 1, "text": answer}))
```

`tests/fake_agent.py` is a complete, deterministic reference implementation.

## Expected answer shape

The system prompt instructs the agent to return one JSON object:
`intent` (`bug_report`/`question`/`other`), `reply`, `language` (input language) and `bug`
(title, component, category, page_url, api_endpoints, http_method, status codes, description,
steps, actual/expected results, severity + justification, impact, evidence, confirmed facts,
hypotheses, missing information, clarification questions, and a `pattern` assessment with a
proposed prevention `rule`). Unknown keys are ignored; wrong shapes are coerced; invalid values
(e.g. an unknown severity or a malformed pattern id) fall back to safe defaults.
