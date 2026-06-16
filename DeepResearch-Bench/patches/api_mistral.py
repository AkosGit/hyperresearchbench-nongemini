"""Mistral-backed LLM client for DeepResearch-Bench (RACE + FACT).

Drop-in replacement for the upstream `deep_research_bench/utils/api.py`.
Exposes the exact same surface the rest of the evaluator imports
(`AIClient`, `call_model`, `scrape_url`, `Model`, `FACT_Model`,
`WebScrapingJinaTool`, `READ_API_KEY`) so RACE
(`deepresearch_bench_race.py`, `clean_article.py`, `generate_criteria.py`)
and FACT (`utils/extract.py`, `utils/deduplicate.py`, `utils/validate.py`)
work unchanged.

`setup.sh` copies this file over `deep_research_bench/utils/api.py` after
cloning the upstream repo, so the swap survives a fresh clone.

Backend selection via env `LLM_BACKEND` (default: mistral):

  mistral (default):
    MISTRAL_API_KEY (required)
    MISTRAL_BASE_URL     (default: https://api.mistral.ai/v1)
    RACE_MODEL           (default: mistral-large-latest)   # quality judge
    FACT_MODEL           (default: mistral-small-latest)   # cheap citation checker
    MISTRAL_TEMPERATURE  (default: 0.0 — deterministic judging)
    MAX_OUTPUT_TOKENS    (default: 8192)

  openrouter / openai (retained from upstream so the harness still works
  with GPT-5.x if MISTRAL is swapped out):
    OPENROUTER_API_KEY / OPENAI_API_KEY
    RACE_MODEL / FACT_MODEL default to the gpt-5.x ids.

Why a dedicated `mistral` backend and not just `LLM_BACKEND=openai` with a
Mistral base_url? Two payload differences:
  1. Mistral's chat/completions takes `max_tokens`, not `max_completion_tokens`.
  2. Mistral rejects OpenAI's `reasoning_effort` field.
So the payload is branched per backend below. `finish_reason` values are
normalized ("model_length" -> "length") so ArticleCleaner's recursive
chunk-on-truncation logic keeps working.
"""
import os
import random
import threading
import time
from typing import Optional, Dict, Any, Tuple, Union
import requests
import logging


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)


# ── Backend selection ──────────────────────────────────────────────
LLM_BACKEND = os.environ.get("LLM_BACKEND", "mistral").lower()

_BACKEND_DEFAULTS = {
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "key_env":  "MISTRAL_API_KEY",
        "base_env": "MISTRAL_BASE_URL",
        "race":     "mistral-large-latest",
        "fact":     "mistral-small-latest",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env":  "OPENROUTER_API_KEY",
        "base_env": "OPENROUTER_BASE_URL",
        "race":     "openai/gpt-5.5",
        "fact":     "openai/gpt-5.4-mini",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env":  "OPENAI_API_KEY",
        "base_env": "OPENAI_BASE_URL",
        "race":     "gpt-5.5",
        "fact":     "gpt-5.4-mini",
    },
}

if LLM_BACKEND not in _BACKEND_DEFAULTS:
    raise ValueError(
        f"Unknown LLM_BACKEND={LLM_BACKEND!r}; expected one of "
        f"{list(_BACKEND_DEFAULTS)}"
    )

_BACKEND = _BACKEND_DEFAULTS[LLM_BACKEND]
_BASE_URL = os.environ.get(_BACKEND["base_env"], _BACKEND["base_url"])
_KEY_ENV = _BACKEND["key_env"]
API_KEY = os.environ.get(_KEY_ENV, "")

# Public module-level model identifiers — same names as the upstream code
# so downstream imports (`from utils.api import Model, FACT_Model`) don't break.
Model = os.environ.get("RACE_MODEL", _BACKEND["race"])
FACT_Model = os.environ.get("FACT_MODEL", _BACKEND["fact"])

# Jina is unchanged — citation scraping (FACT) still uses it.
READ_API_KEY = os.environ.get("JINA_API_KEY", "")

# ── Generation config ─────────────────────────────────────────────
# Mistral chat models cap completion tokens lower than gpt-5.x; 8192 is a safe
# default that won't be rejected. Cleaning long article chunks just recurses
# (ArticleCleaner chunks on a "length" finish_reason). Override if your model
# supports more.
_DEFAULT_MAX_TOKENS = "8192" if LLM_BACKEND == "mistral" else "64000"
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", _DEFAULT_MAX_TOKENS))
HTTP_TIMEOUT_S = int(os.environ.get("LLM_HTTP_TIMEOUT", "600"))

# Deterministic judging by default for Mistral (it accepts temperature, unlike
# gpt-5.x reasoning models). Lower = more reproducible RACE/FACT scores.
MISTRAL_TEMPERATURE = float(os.environ.get("MISTRAL_TEMPERATURE", "0.0"))

# ── Rate-limit resilience (free-tier friendly) ─────────────────────
# EVERY request — RACE cleaning + scoring AND FACT extract/dedup/validate —
# funnels through _post(), so retrying here protects both test paths uniformly.
# (This matters because utils/extract.py doesn't wrap its call_model in a retry,
# so the client is the only safe place to absorb a 429.) On HTTP 429 we honor
# the Retry-After header; otherwise exponential backoff with jitter. Transient
# 5xx and network errors are retried too. With these defaults a sustained
# 1-request/second free-tier limit is absorbed transparently: no RACE score and
# no FACT citation is dropped — the run just takes longer.
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "8"))
LLM_RETRY_BASE = float(os.environ.get("LLM_RETRY_BASE", "1.5"))   # seconds, doubles each attempt
LLM_RETRY_CAP = float(os.environ.get("LLM_RETRY_CAP", "60"))      # max seconds per backoff sleep
# Optional client-side throttle: minimum seconds between requests (0 = off).
# Set ~1.1 on the Mistral free tier (1 req/s) to avoid wasted 429 round-trips.
# Threadsafe within a process (covers RACE's thread pool and single-process FACT);
# with multi-process FACT it throttles per worker, and backoff covers the rest.
LLM_MIN_INTERVAL = float(os.environ.get("LLM_MIN_INTERVAL", "0"))

_rate_lock = threading.Lock()
_last_request_ts = [0.0]


def _throttle() -> None:
    if LLM_MIN_INTERVAL <= 0:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = _last_request_ts[0] + LLM_MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_request_ts[0] = time.monotonic()


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    """Seconds to wait before the next attempt. A server-directed Retry-After is
    obeyed as-is (bounded by a generous hard ceiling so a bogus value can't hang
    the run); otherwise exponential backoff with jitter, capped at LLM_RETRY_CAP."""
    if retry_after:
        try:
            return min(300.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return min(LLM_RETRY_CAP, LLM_RETRY_BASE * (2 ** attempt)) + random.uniform(0, 0.75)

# reasoning_effort only applies to the OpenAI-compatible reasoning backends.
_STAGE_CFG = {
    "clean": {"reasoning_effort": "low"},
    "score": {"reasoning_effort": "medium"},
    "fact":  {"reasoning_effort": "low"},
}


def _resolve_stage(stage: Optional[str]) -> Dict[str, str]:
    if stage is None:
        return _STAGE_CFG["score"]
    if stage not in _STAGE_CFG:
        raise ValueError(f"Unknown stage={stage!r}; expected {list(_STAGE_CFG)}")
    return _STAGE_CFG[stage]


def _normalize_finish_reason(reason: Optional[str]) -> str:
    """Map provider-specific truncation reasons to the canonical 'length' that
    ArticleCleaner watches for."""
    if reason in ("model_length", "max_tokens", "length"):
        return "length"
    return reason or "stop"


class AIClient:
    """OpenAI-compatible chat-completions client (Mistral by default).

    `generate` has two call shapes (both used by the evaluator):

      1) Simple (RACE scoring, criteria generation):
            text = client.generate(user_prompt, system_prompt="")

      2) Metadata-aware (ArticleCleaner):
            text, stop_reason = client.generate(
                user_prompt, system_prompt="",
                return_metadata=True, stage="clean",
            )
         `stop_reason` is the normalized finish_reason; "length" triggers
         recursive chunking upstream.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or API_KEY
        if not self.api_key:
            raise ValueError(
                f"API key not provided! Set env {_KEY_ENV} for backend "
                f"{LLM_BACKEND}."
            )
        self.model = model or Model
        self.base_url = _BASE_URL.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if LLM_BACKEND == "openrouter":
            h["HTTP-Referer"] = os.environ.get(
                "OPENROUTER_REFERER",
                "https://github.com/Ayanami0730/deep_research_bench",
            )
            h["X-Title"] = os.environ.get("OPENROUTER_TITLE", "DRB-Mistral")
        return h

    def _build_messages(self, user_prompt: str, system_prompt: str):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _build_payload(self, model_to_use: str, messages, stage_cfg) -> Dict[str, Any]:
        if LLM_BACKEND == "mistral":
            # Mistral chat/completions: max_tokens, optional temperature,
            # NO reasoning_effort, NO max_completion_tokens.
            return {
                "model": model_to_use,
                "messages": messages,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": MISTRAL_TEMPERATURE,
            }
        # openai / openrouter (gpt-5.x reasoning models)
        return {
            "model": model_to_use,
            "messages": messages,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "reasoning_effort": stage_cfg["reasoning_effort"],
        }

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        last_err = "unknown error"
        for attempt in range(LLM_MAX_RETRIES + 1):
            _throttle()
            try:
                resp = requests.post(url, headers=self._headers(), json=payload,
                                     timeout=HTTP_TIMEOUT_S)
            except requests.exceptions.RequestException as e:
                last_err = f"network error: {e}"
                if attempt < LLM_MAX_RETRIES:
                    time.sleep(_retry_delay(attempt, None))
                    continue
                raise Exception(
                    f"{LLM_BACKEND} request failed after {LLM_MAX_RETRIES} retries: {last_err}"
                )

            if resp.status_code == 200:
                return resp.json()

            # Retry on rate-limit (429) and transient server errors (5xx)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"{resp.status_code}: {resp.text[:200]}"
                if attempt < LLM_MAX_RETRIES:
                    delay = _retry_delay(attempt, resp.headers.get("Retry-After"))
                    logger.warning(
                        "%s %s — retry %d/%d in %.1fs",
                        LLM_BACKEND, resp.status_code, attempt + 1, LLM_MAX_RETRIES, delay,
                    )
                    time.sleep(delay)
                    continue

            # Non-retryable (other 4xx) or retries exhausted
            raise Exception(
                f"{LLM_BACKEND} chat/completions {resp.status_code}: "
                f"{resp.text[:500]}"
            )
        raise Exception(
            f"{LLM_BACKEND} chat/completions failed after {LLM_MAX_RETRIES} retries: {last_err}"
        )

    def generate(
        self,
        user_prompt: str,
        system_prompt: str = "",
        model: Optional[str] = None,
        return_metadata: bool = False,
        stage: Optional[str] = None,
    ) -> Union[str, Tuple[str, str]]:
        model_to_use = model or self.model
        stage_cfg = _resolve_stage(stage)
        messages = self._build_messages(user_prompt, system_prompt)
        payload = self._build_payload(model_to_use, messages, stage_cfg)

        try:
            data = self._post(payload)
        except Exception as e:
            raise Exception(f"Failed to generate content: {e}")

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
            stop_reason = _normalize_finish_reason(choice.get("finish_reason", "stop"))
        except (KeyError, IndexError, TypeError) as e:
            raise Exception(f"Malformed response from {LLM_BACKEND}: {data!r} ({e})")

        if return_metadata:
            return content, stop_reason
        return content


# ── FACT pipeline helper ──────────────────────────────────────────
def call_model(user_prompt: str) -> str:
    """Default LLM call for the FACT pipeline (extract / dedup / validate).
    Uses the cheap FACT_Model.
    """
    client = AIClient(model=FACT_Model)
    return client.generate(user_prompt, stage="fact")


# ── Jina scraping (unchanged from upstream) ──────────────────────
class WebScrapingJinaTool:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("JINA_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Jina API key not provided! Please set JINA_API_KEY environment variable."
            )

    def __call__(self, url: str) -> Dict[str, Any]:
        try:
            jina_url = f'https://r.jina.ai/{url}'
            headers = {
                "Accept": "application/json",
                'Authorization': self.api_key,
                'X-Timeout': "60000",
                "X-With-Generated-Alt": "true",
            }
            response = requests.get(jina_url, headers=headers)

            if response.status_code != 200:
                raise Exception(f"Jina AI Reader Failed for {url}: {response.status_code}")

            response_dict = response.json()

            return {
                'url': response_dict['data']['url'],
                'title': response_dict['data']['title'],
                'description': response_dict['data']['description'],
                'content': response_dict['data']['content'],
                'publish_time': response_dict['data'].get('publishedTime', 'unknown')
            }

        except Exception as e:
            logger.error(str(e))
            return {
                'url': url,
                'content': '',
                'error': str(e)
            }


# Lazy-init Jina tool: only instantiate when JINA_API_KEY is actually needed.
_jina_tool: Optional[WebScrapingJinaTool] = None


def scrape_url(url: str) -> Dict[str, Any]:
    global _jina_tool
    if _jina_tool is None:
        _jina_tool = WebScrapingJinaTool()
    return _jina_tool(url)


if __name__ == "__main__":
    print(f"Backend:    {LLM_BACKEND}")
    print(f"Base URL:   {_BASE_URL}")
    print(f"RACE Model: {Model}")
    print(f"FACT Model: {FACT_Model}")
    print(f"Max tokens: {MAX_OUTPUT_TOKENS}")
    if LLM_BACKEND == "mistral":
        print(f"Temperature:{MISTRAL_TEMPERATURE}")
    print(f"Key env:    {_KEY_ENV} (set={bool(API_KEY)})")
