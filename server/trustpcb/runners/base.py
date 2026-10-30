"""Runner interface (contracts.md). Tools are plain Python; a runner adapts them to its model."""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

Emit = Callable[[str, dict], Awaitable[None]]
Interact = Callable[[str, dict], Awaitable[dict]]


@dataclass
class RunRequest:
    session_id: str | None
    system_prompt: str
    user_text: str
    tools: Any  # review.tools.ToolRegistry; see tool_specs() for what a runner may rely on
    max_turns: int
    profile: dict = field(default_factory=dict)


class Runner(Protocol):
    async def run(self, request: RunRequest, emit: Emit, interact: Interact) -> None: ...
    async def cancel(self) -> None: ...
    async def close(self) -> None: ...


def tool_specs(registry: Any) -> list[Any]:
    """The ToolSpec objects of a registry, whichever way it exposes them.

    Accepts a registry with `specs()` / `specs` / `tools` (list or dict), or any iterable of ToolSpec.
    Each spec has name, description, input_schema and an async handler(args) -> ToolResult.
    """
    for name in ('specs', 'tools'):
        candidate = getattr(registry, name, None)
        if candidate is None:
            continue
        if callable(candidate):
            candidate = candidate()
        if isinstance(candidate, dict):
            candidate = candidate.values()
        return list(candidate)
    if isinstance(registry, Iterable):
        return list(registry)
    raise TypeError('Unsupported tool registry')


async def call_tool(registry: Any, name: str, args: dict) -> Any:
    """Run a registered tool by name. Returns its ToolResult; raises KeyError for an unknown tool."""
    for spec in tool_specs(registry):
        if spec.name == name:
            result = spec.handler(args)
            return await result if inspect.isawaitable(result) else result
    raise KeyError(name)


def result_payload(result: Any) -> dict:
    """ToolResult as the JSON stored in a tool_result event (image bytes are summarised, never inlined)."""
    images = getattr(result, 'images', None) or []
    payload = {
        'content': getattr(result, 'text', str(result)),
        'is_error': bool(getattr(result, 'is_error', False)),
        'images': [{'mime': image.get('mime'), 'bytes_b64': len(image.get('data_b64', ''))} for image in images],
    }
    structured = getattr(result, 'structured', None)
    if structured is not None:
        payload['structured'] = structured
    return payload
