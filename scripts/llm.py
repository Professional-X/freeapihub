"""LLM provider abstraction for FreeAPIHub.

Provider-agnostic adapter (spec #48). The initial implementation speaks the
OpenAI-compatible /chat/completions protocol, which covers OpenAI, Groq,
OpenRouter, Together, Mistral, DeepSeek, local llama.cpp/vLLM servers, etc.

Configuration (all via environment, never in files):
    LLM_API_KEY    - required for research; without it the pipeline still runs
                     but skips LLM-based enrichment (deterministic fallback).
    LLM_BASE_URL   - optional, default https://api.openai.com/v1
    LLM_MODEL      - optional model name override (also in config/site.json)

The API key is never logged, never written to disk, and never sent anywhere
except the configured provider endpoint.
"""

from __future__ import annotations

import json
import re
import time

import requests

from utils import LOG, env, load_config

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMError(Exception):
    """Raised when the provider fails after retries or returns unusable JSON."""


class LLMProvider:
    """Interface: swap in any provider by subclassing these two methods."""

    name = "base"

    def generate_structured(self, system: str, user: str, max_tokens: int = 1400) -> dict:
        raise NotImplementedError

    def generate_text(self, system: str, user: str, max_tokens: int = 1400) -> str:
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    name = "openai-compatible"

    def __init__(self, api_key: str, base_url: str = "", model: str = "",
                 timeout: int = 90, temperature: float = 0.2):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self.temperature = temperature

    # -- internals ----------------------------------------------------------

    def _post(self, system: str, user: str, max_tokens: int, json_mode: bool) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        last_error = None
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() \
                        else min(2 ** attempt * 2, 15)
                    last_error = f"provider HTTP {resp.status_code}"
                    time.sleep(delay)
                    continue
                if resp.status_code == 400 and json_mode:
                    # Some providers reject response_format; retry without it.
                    payload.pop("response_format", None)
                    json_mode = False
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or ""
                return content
            except requests.exceptions.Timeout:
                last_error = "provider timeout"
                time.sleep(min(2 ** attempt * 2, 15))
            except (requests.exceptions.RequestException, KeyError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                time.sleep(min(2 ** attempt * 2, 15))
        raise LLMError(last_error or "provider failed")

    @staticmethod
    def _extract_json(text: str) -> dict:
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        raise LLMError("no valid JSON object found in provider response")

    # -- public interface ---------------------------------------------------

    def generate_text(self, system: str, user: str, max_tokens: int = 1400) -> str:
        return self._post(system, user, max_tokens, json_mode=False)

    def generate_structured(self, system: str, user: str, max_tokens: int = 1400) -> dict:
        raw = self._post(system, user, max_tokens, json_mode=True)
        try:
            return self._extract_json(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"invalid JSON from provider: {exc}") from exc


def get_provider(cfg: dict | None = None) -> LLMProvider | None:
    """Return a provider if LLM_API_KEY is configured, else None.
    A None provider means: run deterministically, skip enrichment."""
    cfg = cfg or load_config()
    api_key = env("LLM_API_KEY")
    if not api_key:
        LOG.warning("LLM_API_KEY not set - running in deterministic-only mode (no LLM calls).")
        return None
    model = cfg.get("llm_model") or DEFAULT_MODEL
    provider = OpenAICompatibleProvider(
        api_key=api_key,
        base_url=env("LLM_BASE_URL"),
        model=model,
        timeout=int(cfg.get("llm_timeout_seconds", 90)),
        temperature=float(cfg.get("llm_temperature", 0.2)),
    )
    LOG.info("LLM provider ready (model=%s, base=%s, key=***masked***)",
             provider.model, provider.base_url)
    return provider
