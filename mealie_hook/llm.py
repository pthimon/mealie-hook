"""OpenAI-compatible chat client for the local llama-server, structured output only."""

import logging
import time
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url: str, model: str = "qwen", timeout: float = 600):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.model = model

    def structured(self, messages: list[dict], reply: type[M], name: str,
                   temperature: float = 0.2, retries: int = 2) -> M:
        """Return the model's reply validated as `reply`.

        The pydantic model's JSON schema goes to llama-server as `response_format`, which
        compiles it into a grammar, so the output always parses; validation then catches
        anything the grammar cannot express.

        Thinking is switched off per request. Qwen3.5 is a reasoning model and, left to
        itself, spends ~16k tokens thinking about a 20-line ingredient list (six minutes on
        this box). llama.cpp ignores `reasoning_effort`; only the chat-template kwarg works.
        Doing it per request leaves the shared server's defaults alone for its other users.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name,
                                                "schema": reply.model_json_schema()}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        last: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                r = self.http.post("/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
                result = reply.model_validate_json(data["choices"][0]["message"]["content"])
            except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as e:
                last = e
                log.warning("llm %s attempt %d failed: %s", name, attempt + 1, e)
                continue
            usage = data.get("usage") or {}
            log.info("llm %s: %.1fs, %s prompt + %s completion tokens", name,
                     time.monotonic() - t0, usage.get("prompt_tokens"),
                     usage.get("completion_tokens"))
            return result
        raise LLMError(f"{name}: no valid reply after {retries + 1} attempts: {last}")


def load_prompt(prompts_dir: Path, name: str) -> str:
    return (Path(prompts_dir) / f"{name}.md").read_text().strip()
