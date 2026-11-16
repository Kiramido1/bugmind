#!/usr/bin/env python3
"""Deterministic stand-in for an AI agent, speaking the BugMind custom-agent contract.

Behaviour is selected with FAKE_AGENT_MODE:
  bug (default)  return a bug analysis; pattern relation from FAKE_AGENT_RELATION
  question       return a question answer
  garbage        return non-JSON text
  envelope       wrap the answer in {"bugmind_protocol": 1, "text": ...}
  error          print an error envelope
  fail           exit 7 with a message on stderr
  auth           exit 1 with an authentication message
  sleep          sleep 30s (for timeout tests)
"""

import json
import os
import sys
import time

mode = os.environ.get("FAKE_AGENT_MODE", "bug")
raw = sys.stdin.read() if mode != "file" else ""
if mode == "sleep":
    time.sleep(30)
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(7)
if mode == "auth":
    print("Error: not logged in. Please run /login", file=sys.stderr)
    sys.exit(1)
if mode == "error":
    print(json.dumps({"bugmind_protocol": 1, "error": "model overloaded"}))
    sys.exit(0)
if mode == "garbage":
    print("I am not JSON at all")
    sys.exit(0)

req = json.loads(raw) if raw.strip().startswith("{") else {"prompt": raw}
if os.environ.get("FAKE_AGENT_RECORD"):  # lets tests inspect exactly what reached the agent
    with open(os.environ["FAKE_AGENT_RECORD"], "w") as fh:
        json.dump({"images": [{k: v for k, v in i.items() if k != "data_base64"} | {"bytes": len(i["data_base64"])}
                              for i in req.get("images", [])], "prompt": req.get("prompt", "")}, fh)
assert "untrusted" in req.get("system", "") or mode in ("question",), "system prompt missing"

relation = os.environ.get("FAKE_AGENT_RELATION", "new")
matched = os.environ.get("FAKE_AGENT_MATCH")
name = os.environ.get("FAKE_AGENT_PATTERN", "Unvalidated ordering parameter reaches query builder")

if mode == "question":
    answer = {"intent": "question", "reply": "PAT-001 covers ordering validation.", "language": "en", "bug": None}
else:
    answer = {
        "intent": "bug_report",
        "reply": "Analyzed.",
        "language": "ar" if any("؀" <= c <= "ۿ" for c in req.get("prompt", "")) else "en",
        "bug": {
            "title": "[Orders API] Empty `ordering` returns 500 instead of 400",
            "component": "Orders API",
            "category": "Input Validation",
            "page_url": "N/A",
            "api_endpoints": ["/api/v1/orders?ordering="],
            "http_method": "GET",
            "actual_status_code": "500",
            "expected_status_code": "400",
            "description": "The endpoint returns 500 for an empty ordering value.",
            "steps_to_reproduce": "1. Send GET /api/v1/orders?ordering=\n2. Observe the status",
            "actual_result": ["500 Internal Server Error"],
            "expected_result": ["400 validation error"],
            "severity": os.environ.get("FAKE_AGENT_SEVERITY", "medium"),
            "severity_justification": "Server error on user input.",
            "evidence": ["GET /api/v1/orders?ordering= -> 500"],
            "clarification_questions": json.loads(os.environ.get("FAKE_AGENT_QUESTIONS", "[]")),
            "pattern": {
                "relation": relation,
                "matched_pattern_id": matched,
                "pattern_name": name,
                "pattern_category": "input-validation",
                "pattern_description": "Query parameters are not validated before query construction.",
                "signature": "ordering, validation, 500",
                "rule": {
                    "title": os.environ.get("FAKE_AGENT_RULE_TITLE", "Validate ordering parameters"),
                    "problem": "Empty ordering values cause unhandled exceptions.",
                    "required_behavior": json.loads(os.environ.get(
                        "FAKE_AGENT_RULE_ITEMS", '["Validate ordering at the API boundary", "Reject empty ordering fields"]')),
                    "regression_tests": ["Test empty ordering values"],
                },
            },
        },
    }

text = "Here is the analysis:\n```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"
if mode == "envelope":
    text = json.dumps({"bugmind_protocol": 1, "text": json.dumps(answer)})
print(text)
