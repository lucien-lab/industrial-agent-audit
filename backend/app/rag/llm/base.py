"""LLM 抽象：一次对话调用 = 明确的成功或明确的失败，不允许静默降级成假回答。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system / user / assistant
    content: str


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    elapsed_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: int = 1
    metadata: dict[str, object] = field(default_factory=dict)


class ChatModel(Protocol):
    provider: str
    model: str

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...
