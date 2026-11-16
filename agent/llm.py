"""Provider-agnostic LLM layer with usage, cost and budget tracking.

Providers (set LLM_PROVIDER):
  anthropic  -> Anthropic Messages API           (ANTHROPIC_API_KEY or LLM_API_KEY, LLM_MODEL)
  openai     -> any OpenAI-compatible endpoint  (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)
                works with OpenAI, Azure-compatible gateways, Ollama, vLLM, LM Studio
  offline    -> agent/offline.py: a deterministic heuristic stand-in. It exercises the whole
                pipeline without a key. Its numbers are NOT research results.

Every agent calls `llm.complete(role, system, messages, context=...)`. Real providers
see only `system` and `messages`; `context` is the same information in structured
form, which only the offline stand-in reads.

Only the standard library is used for HTTP (urllib), so the core has no SDK dependency.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Usage:
    """Per-investigation meter. Shared by every agent in one investigation."""
    max_tokens: int = 60000
    max_tool_calls: int = 25
    max_seconds: float = 300
    price_in: float = 0.0          # US$ per million input tokens
    price_out: float = 0.0         # US$ per million output tokens
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    started: float = field(default_factory=time.time)
    by_role: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def cost_usd(self) -> float:
        return (self.tokens_in * self.price_in + self.tokens_out * self.price_out) / 1e6

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def add_llm(self, role: str, tin: int, tout: int) -> None:
        with self._lock:
            self.llm_calls += 1
            self.tokens_in += tin
            self.tokens_out += tout
            r = self.by_role.setdefault(role, {"calls": 0, "tokens": 0})
            r["calls"] += 1
            r["tokens"] += tin + tout

    def add_tool(self) -> None:
        with self._lock:
            self.tool_calls += 1

    def check(self) -> None:
        if self.tokens > self.max_tokens:
            raise BudgetExceeded(f"token budget {self.max_tokens} exceeded")
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceeded(f"tool-call budget {self.max_tool_calls} exceeded")
        if self.elapsed > self.max_seconds:
            raise BudgetExceeded(f"time budget {self.max_seconds}s exceeded")

    def as_dict(self) -> dict:
        return {"llm_calls": self.llm_calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "tokens": self.tokens, "tool_calls": self.tool_calls, "cost_usd": round(self.cost_usd, 6),
                "latency_s": round(self.elapsed, 3), "by_role": self.by_role}


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int


class LLM:
    name = "base"
    model = ""

    def __init__(self, usage: Usage | None = None):
        self.usage = usage or Usage()

    def with_usage(self, usage: Usage) -> "LLM":
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.usage = usage
        return clone

    def complete(self, role: str, system: str, messages: list[dict], *, temperature: float = 0.0,
                 max_tokens: int = 1200, context: dict | None = None) -> str:
        self.usage.check()
        resp = self._complete(role, system, messages, temperature, max_tokens, context or {})
        self.usage.add_llm(role, resp.tokens_in, resp.tokens_out)
        return resp.text

    def _complete(self, role, system, messages, temperature, max_tokens, context) -> LLMResponse:
        raise NotImplementedError


def _post(url: str, headers: dict, body: dict, retries: int = 4, timeout: float = 120) -> dict:
    data = json.dumps(body).encode()
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"LLM HTTP {e.code}: {e.read()[:300]!r}") from e
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


class AnthropicLLM(LLM):
    name = "anthropic"

    def __init__(self, model: str, api_key: str, usage: Usage | None = None,
                 base_url: str = "https://api.anthropic.com"):
        super().__init__(usage)
        self.model, self.key, self.base = model, api_key, base_url.rstrip("/")

    def _complete(self, role, system, messages, temperature, max_tokens, context):
        out = _post(f"{self.base}/v1/messages",
                    {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                    {"model": self.model, "system": system, "messages": messages,
                     "temperature": temperature, "max_tokens": max_tokens})
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        u = out.get("usage", {})
        return LLMResponse(text, u.get("input_tokens", 0), u.get("output_tokens", 0))


class OpenAICompatLLM(LLM):
    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str, usage: Usage | None = None):
        super().__init__(usage)
        self.model, self.key, self.base = model, api_key, base_url.rstrip("/")

    def _complete(self, role, system, messages, temperature, max_tokens, context):
        headers = {"authorization": f"Bearer {self.key}"} if self.key else {}
        out = _post(f"{self.base}/chat/completions", headers,
                    {"model": self.model, "messages": [{"role": "system", "content": system}, *messages],
                     "temperature": temperature, "max_tokens": max_tokens})
        text = out["choices"][0]["message"].get("content") or ""
        u = out.get("usage", {})
        return LLMResponse(text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from the repo's .env into os.environ (existing variables win)."""
    path = path or Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def from_env(usage: Usage | None = None) -> LLM:
    load_dotenv()
    provider = os.environ.get("LLM_PROVIDER", "offline").lower()
    model = os.environ.get("LLM_MODEL", "")
    usage = usage or Usage()
    usage.price_in = float(os.environ.get("LLM_PRICE_IN_PER_MTOK", usage.price_in or 0))
    usage.price_out = float(os.environ.get("LLM_PRICE_OUT_PER_MTOK", usage.price_out or 0))
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY", "")
        if not key or not model:
            raise RuntimeError("set ANTHROPIC_API_KEY (or LLM_API_KEY) and LLM_MODEL for the anthropic provider")
        return AnthropicLLM(model, key, usage)
    if provider == "openai":
        if not model:
            raise RuntimeError("set LLM_MODEL (and LLM_BASE_URL / LLM_API_KEY) for the openai provider")
        return OpenAICompatLLM(model, os.environ.get("LLM_API_KEY", ""),
                               os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"), usage)
    if provider == "offline":
        from agent.offline import OfflineLLM
        return OfflineLLM(usage)
    raise RuntimeError(f"unknown LLM_PROVIDER {provider}")


# ------------------------------------------------------------------ JSON helpers
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str):
    """Extract the first JSON object or array from model output (tolerates prose and code fences)."""
    dec = json.JSONDecoder()
    m = _FENCE.search(text)
    for chunk in ([m.group(1)] if m else []) + [text]:
        for i, ch in enumerate(chunk):
            if ch in "{[":
                try:
                    obj, _ = dec.raw_decode(chunk, i)
                    return obj
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"no JSON found in model output: {text[:200]!r}")
