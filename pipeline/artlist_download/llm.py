"""Injectable LLM client.

The agent only needs `create(**request) -> response` where the response has
`.content` (list of blocks with `.type`, and for tool calls `.id`, `.name`,
`.input`, optional `.toolset_name`), `.stop_reason` and `.usage`.
`AnthropicLLM` is the real implementation; tests pass a scripted fake.
"""
from __future__ import annotations

from typing import Any, Protocol

from .config import FALLBACK_MODELS

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMClient(Protocol):
    def create(self, **request: Any) -> Any: ...


class AnthropicLLM:
    """Claude Messages API with the computer toolset.

    - Computer use goes through `computer_toolset_20260801` (GA, no beta header);
      Claude Opus 5.5 rejects the older `computer_20251124` tool on the Claude API.
    - Thinking is always adaptive on Opus 5.5, so `thinking` is omitted; depth is
      set with `output_config.effort`.
    - Server-side refusal fallback (`fallbacks: "default"`) is on by default for
      models that support it; disable with use_fallbacks=False (--no-fallbacks).
    """

    def __init__(self, model: str, *, use_fallbacks: bool = True, timeout_s: float = 300.0, client: Any = None):
        import anthropic  # imported lazily so tests / dry-run don't need it

        self.model = model
        self.use_fallbacks = use_fallbacks and model in FALLBACK_MODELS
        self._client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=3)

    def create(self, **request: Any) -> Any:
        request.setdefault("model", self.model)
        if self.use_fallbacks:
            return self._client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **request)
        return self._client.messages.create(**request)


def block_get(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def block_input(block: Any) -> dict:
    inp = block_get(block, "input", {}) or {}
    if hasattr(inp, "model_dump"):
        inp = inp.model_dump(exclude_none=True)
    return dict(inp)
