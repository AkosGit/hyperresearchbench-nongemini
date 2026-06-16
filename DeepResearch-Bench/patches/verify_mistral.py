#!/usr/bin/env python3
"""Offline self-test for the Mistral evaluator client (api_mistral.py).

Mocks the network — makes NO real API calls — and asserts that the client
builds correct Mistral chat/completions requests:
  - base_url / auth / endpoint
  - max_tokens (NOT max_completion_tokens), and NO reasoning_effort
  - RACE uses mistral-large-latest, FACT (call_model) uses mistral-small-latest
  - finish_reason "model_length" normalizes to "length"

Usage:
    python patches/verify_mistral.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("LLM_BACKEND", "mistral")
os.environ.setdefault("MISTRAL_API_KEY", "dummy-key-for-offline-test")

spec = importlib.util.spec_from_file_location(
    "api_mistral", os.path.join(HERE, "api_mistral.py")
)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

captured = {}


class _FakeResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": '{"ok": true}'},
                             "finish_reason": "model_length"}]}


def _fake_post(url, headers=None, json=None, timeout=None):
    captured["url"] = url
    captured["headers"] = headers
    captured["payload"] = json
    return _FakeResp()


api.requests.post = _fake_post


def main() -> int:
    print(f"Backend={api.LLM_BACKEND}  base={api._BASE_URL}")
    print(f"RACE={api.Model}  FACT={api.FACT_Model}  max_tokens={api.MAX_OUTPUT_TOKENS}")

    # RACE-style call
    text, stop = api.AIClient().generate(
        "score this", system_prompt="be strict", return_metadata=True, stage="score"
    )
    p = captured["payload"]
    assert api._BASE_URL == "https://api.mistral.ai/v1", api._BASE_URL
    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert "max_tokens" in p and "max_completion_tokens" not in p, "must use max_tokens"
    assert "reasoning_effort" not in p, "mistral must not send reasoning_effort"
    assert p["model"] == "mistral-large-latest", p["model"]
    assert stop == "length", f"model_length should normalize to 'length', got {stop!r}"

    # FACT-style call
    api.call_model("extract citations")
    assert captured["payload"]["model"] == "mistral-small-latest", "call_model must use FACT_Model"

    print("\nALL CHECKS PASSED ✓  (no real API calls were made)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
