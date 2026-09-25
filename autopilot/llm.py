"""Provider-agnostic LLM access.

* Anthropic keys use the official ``anthropic`` SDK (streaming, adaptive thinking
  left at the model default, ``output_config.effort`` for cost/quality tuning).
* OpenRouter / OpenAI / DeepSeek keys use the ``openai`` SDK against the
  provider's OpenAI-compatible endpoint.

Every call is accounted in ``llm_usage`` with a rough USD estimate.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


# USD per 1M tokens (input, output). Unknown models -> (0, 0): tokens are still logged.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}

DEFAULT_MODELS = {
    # provider: (main, fast)
    "anthropic": ("claude-opus-5", "claude-haiku-4-5"),
    "openrouter": ("anthropic/claude-opus-5", "anthropic/claude-haiku-4.5"),
    "openai": ("gpt-5", "gpt-5-mini"),
    "deepseek": ("deepseek-chat", "deepseek-chat"),
}

# Fallback chains when a provider rejects a model id (404 / unknown model).
FALLBACK_CHAINS = {
    "openrouter": ["anthropic/claude-sonnet-4.5", "openai/gpt-5", "google/gemini-2.5-pro", "deepseek/deepseek-chat-v3.1"],
    "openai": ["gpt-5", "gpt-4.1", "gpt-4o"],
    "deepseek": ["deepseek-chat"],
    "anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-sonnet-4-6"],
}

EFFORT_CAPABLE_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
                           "claude-opus-4-6", "claude-sonnet-4-6", "claude-fable")


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array from an LLM answer (tolerates fences/prose)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            chunk = text[start:end + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                # Common LLM slip: trailing commas.
                cleaned = re.sub(r",\s*([}\]])", r"\1", chunk)
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    continue
    raise LLMError("Ответ модели не содержит валидный JSON: " + text[:200])


class LLM:
    def __init__(self, settings: Settings, db: Any = None):
        self.settings = settings
        self.db = db
        self.provider = settings.llm_provider
        if not self.provider:
            raise LLMError("LLM-ключ не задан")
        main, fast = DEFAULT_MODELS[self.provider]
        self.model = settings.llm_model or main
        self.model_fast = settings.llm_model_fast or fast
        self._client: Any = None

    # ----------------------------------------------------------------- client
    def _anthropic(self):
        if self._client is None:
            import anthropic

            kwargs: dict[str, Any] = {"api_key": self.settings.anthropic_api_key, "max_retries": 3, "timeout": 600.0}
            if self.settings.http_proxy:
                kwargs["http_client"] = anthropic.DefaultHttpxClient(proxy=self.settings.http_proxy)
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _openai(self):
        if self._client is None:
            from openai import OpenAI

            if self.provider == "openrouter":
                key, base = self.settings.openrouter_api_key, "https://openrouter.ai/api/v1"
            elif self.provider == "deepseek":
                key, base = self.settings.deepseek_api_key, "https://api.deepseek.com"
            else:
                key, base = self.settings.openai_api_key, None
            kwargs: dict[str, Any] = {"api_key": key, "max_retries": 3, "timeout": 600.0}
            if base:
                kwargs["base_url"] = base
            if self.provider == "openrouter":
                kwargs["default_headers"] = {"HTTP-Referer": "https://github.com/Conkormedia/dzen-ai-factory",
                                             "X-Title": "dzen-autopilot"}
            self._client = OpenAI(**kwargs)
        return self._client

    # ------------------------------------------------------------------ calls
    def chat(self, system: str, user: str, *, model: str | None = None, max_tokens: int = 12000,
             temperature: float | None = None, purpose: str = "", effort: str | None = None) -> LLMResult:
        model = model or self.model
        chain = [model] + [m for m in FALLBACK_CHAINS.get(self.provider, []) if m != model]
        last_exc: Exception | None = None
        for candidate in chain:
            try:
                if self.provider == "anthropic":
                    result = self._chat_anthropic(system, user, candidate, max_tokens, temperature, effort)
                else:
                    result = self._chat_openai(system, user, candidate, max_tokens, temperature)
                if candidate != model:
                    log.warning("LLM model fallback: %s -> %s", model, candidate)
                    if self.db is not None:
                        self.db.log_event(f"LLM: модель {model} недоступна, использую {candidate}", kind="llm", level="warn")
                self._account(candidate, purpose, result)
                return result
            except LLMRefusal:
                raise
            except Exception as exc:  # noqa: BLE001 - provider specific errors are heterogeneous
                last_exc = exc
                if not self._is_model_missing(exc):
                    raise LLMError(f"{type(exc).__name__}: {exc}") from exc
                log.warning("Model %s rejected (%s); trying next", candidate, exc)
        raise LLMError(f"Ни одна модель не доступна: {last_exc}")

    def json(self, system: str, user: str, *, purpose: str = "", model: str | None = None, max_tokens: int = 12000,
             effort: str | None = None) -> Any:
        system_json = system + "\n\nОтвечай ТОЛЬКО валидным JSON без пояснений и без markdown-ограждений."
        result = self.chat(system_json, user, model=model, max_tokens=max_tokens, purpose=purpose, effort=effort)
        try:
            return extract_json(result.text)
        except LLMError:
            repair = self.chat(
                "Ты конвертер. Преобразуй текст в валидный JSON той же структуры, ничего не добавляя. Только JSON.",
                result.text[:60000], model=self.model_fast, max_tokens=max_tokens, purpose=purpose + ":repair")
            return extract_json(repair.text)

    # --------------------------------------------------------------- backends
    def _chat_anthropic(self, system: str, user: str, model: str, max_tokens: int, temperature: float | None,
                        effort: str | None) -> LLMResult:
        client = self._anthropic()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if model.startswith(EFFORT_CAPABLE_PREFIXES):
            kwargs["output_config"] = {"effort": effort or self.settings.llm_effort or "medium"}
        elif temperature is not None:
            kwargs["temperature"] = temperature
        with client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise LLMRefusal("Модель отказалась выполнять запрос (stop_reason=refusal)")
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        if message.stop_reason == "max_tokens":
            log.warning("Anthropic response hit max_tokens=%s", max_tokens)
        usage = message.usage
        return LLMResult(text=text, model=message.model or model,
                         input_tokens=int(getattr(usage, "input_tokens", 0) or 0)
                         + int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                         + int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                         output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                         stop_reason=str(message.stop_reason or ""))

    def _chat_openai(self, system: str, user: str, model: str, max_tokens: int, temperature: float | None) -> LLMResult:
        client = self._openai()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        # Newer OpenAI models only accept max_completion_tokens; others accept max_tokens.
        if self.provider == "openai" and (model.startswith("gpt-5") or model.startswith("o")):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            if temperature is not None:
                kwargs["temperature"] = temperature
        completion = client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        text = choice.message.content or ""
        usage = getattr(completion, "usage", None)
        return LLMResult(text=text, model=getattr(completion, "model", model) or model,
                         input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                         output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                         stop_reason=str(getattr(choice, "finish_reason", "") or ""))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _is_model_missing(exc: Exception) -> bool:
        text = str(exc).lower()
        status = getattr(exc, "status_code", None)
        if status == 404:
            return True
        return any(m in text for m in ("model_not_found", "not a valid model", "no endpoints found",
                                        "does not exist", "unknown model", "invalid model", "not_found_error"))

    def _account(self, model: str, purpose: str, result: LLMResult) -> None:
        price_in, price_out = PRICES.get(result.model, PRICES.get(model, (0.0, 0.0)))
        cost = result.input_tokens / 1e6 * price_in + result.output_tokens / 1e6 * price_out
        if self.db is not None:
            try:
                self.db.add_usage(self.provider, result.model, purpose, result.input_tokens, result.output_tokens, cost)
            except Exception:  # noqa: BLE001
                log.exception("usage accounting failed")

    def ping(self) -> str:
        started = time.time()
        result = self.chat("Отвечай одним словом.", "Скажи: ок", model=self.model_fast, max_tokens=20, purpose="ping")
        return f"{self.provider}/{result.model} ответил за {time.time() - started:.1f}s: {result.text.strip()[:40]}"
