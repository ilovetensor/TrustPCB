"""Tool objects and the registry that serves them to a runner (contracts.md).

`ToolSpec` and `ToolResult` are what a runner sees. They are plain Python and know nothing about
any provider. `ToolDef` is the richer record the review keeps (phases, levels, flags);
`ToolRegistry.specs()` turns the allowed ones into specs whose handlers run the full pipeline:

    phase/level gate -> argument check -> budget gate -> handler -> bound the result -> bookkeeping

A runner should read `specs()` again on every model turn, because the allowed set changes when the
phase changes. A call to a tool that is not allowed right now still reaches `ToolRegistry.call` and
answers `not_allowed_in_phase` with the tools that are allowed.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

MAX_RESULT_CHARS = 8000
MAX_STRUCTURED_CHARS = 16000


@dataclass
class ToolResult:
    text: str
    images: list = field(default_factory=list)
    is_error: bool = False
    structured: dict | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Awaitable[ToolResult]]


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Awaitable[ToolResult]]
    phases: frozenset = frozenset()         # review phases where the tool works
    levels: frozenset = frozenset()         # follow-up levels where the tool works
    free: bool = False                      # does not count toward tool-call budgets
    closing: bool = False                   # still works after a budget ended
    image: bool = False                     # returns an image (counts against the image budget)
    trail: Callable[[dict], str] | None = None


class Gate(Protocol):
    """What the registry asks the running session. The review session and the follow-up session implement it."""

    def allowed_names(self) -> list[str]: ...
    def phase_label(self) -> str: ...
    async def before(self, tool: ToolDef, args: dict) -> ToolResult | None: ...
    async def after(self, tool: ToolDef, args: dict, result: ToolResult) -> None: ...


# ---- results -------------------------------------------------------------------------------

def dumps(data: Any) -> str:
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False, default=str)


def ok(data: dict, *, text: str | None = None, images: list | None = None) -> ToolResult:
    """A successful result: compact JSON for the model, the same data as `structured` for the app."""
    return ToolResult(text=text if text is not None else dumps(data), images=images or [], structured=data)


def fail(code: str, message: str, **extra: Any) -> ToolResult:
    data = {'error': code, 'message': message, **extra}
    return ToolResult(text=dumps(data), is_error=True, structured=data)


def bound(result: ToolResult, limit: int = MAX_RESULT_CHARS) -> ToolResult:
    """Keep what the model reads near 8 KB. Lists are paged by their tools; this is the backstop."""
    if len(result.text) > limit:
        cut = result.text[: limit - 120].rsplit(',', 1)[0] if result.text.startswith(('{', '[')) else result.text[: limit - 120]
        result.text = cut + f'... [cut: {len(result.text) - len(cut)} characters not shown; narrow the request or ask for the next page]'
    if result.structured is not None and len(dumps(result.structured)) > MAX_STRUCTURED_CHARS:
        result.structured = {'truncated': True, 'note': 'Structured data was too large to keep.'}
    return result


def page(items: list, number: int = 1, size: int = 10) -> dict:
    """One page of a list, with enough to ask for the next."""
    number = max(1, int(number or 1))
    pages = max(1, -(-len(items) // size))
    return {'items': items[(number - 1) * size: number * size], 'page': number, 'pages': pages, 'total': len(items)}


# ---- argument checking --------------------------------------------------------------------

_TYPES = {
    'string': lambda v: isinstance(v, str), 'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), 'boolean': lambda v: isinstance(v, bool),
    'array': lambda v: isinstance(v, list), 'object': lambda v: isinstance(v, dict),
}


def validate(schema: dict, value: Any, path: str = 'arguments') -> list[str]:
    """Errors for `value` against the JSON-schema subset the tools use. Unknown fields are errors."""
    errors: list[str] = []
    kind = schema.get('type')
    if kind and not _TYPES[kind](value):
        return [f'{path} must be {kind}']
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path} must be one of {schema["enum"]}')
    if isinstance(value, str):
        if 'minLength' in schema and len(value.strip()) < schema['minLength']:
            errors.append(f'{path} must be at least {schema["minLength"]} characters')
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            errors.append(f'{path} must be at most {schema["maxLength"]} characters')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(f'{path} has the wrong format')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            errors.append(f'{path} must be at least {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            errors.append(f'{path} must be at most {schema["maximum"]}')
    if isinstance(value, list):
        if 'minItems' in schema and len(value) < schema['minItems']:
            errors.append(f'{path} needs at least {schema["minItems"]} items')
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            errors.append(f'{path} allows at most {schema["maxItems"]} items')
        if 'items' in schema:
            for index, item in enumerate(value[:200]):
                errors.extend(validate(schema['items'], item, f'{path}[{index}]'))
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        for name in schema.get('required', []):
            if name not in value:
                errors.append(f'{path}.{name} is required')
        for name, item in value.items():
            if name in properties:
                errors.extend(validate(properties[name], item, f'{path}.{name}'))
            elif 'additionalProperties' in schema and isinstance(schema['additionalProperties'], dict):
                errors.extend(validate(schema['additionalProperties'], item, f'{path}.{name}'))
            elif schema.get('additionalProperties') is False:
                errors.append(f'{path}.{name} is not a known field')
    return errors


def obj(properties: dict, required: list[str] | None = None, *, extra: bool = False) -> dict:
    return {'type': 'object', 'properties': properties, 'required': required or [], 'additionalProperties': extra}


STR = {'type': 'string'}
INT = {'type': 'integer'}
NUM = {'type': 'number'}
BOOL = {'type': 'boolean'}


def text(minimum: int = 1, maximum: int = 2000) -> dict:
    return {'type': 'string', 'minLength': minimum, 'maxLength': maximum}


def arr(items: dict, minimum: int = 0, maximum: int = 50) -> dict:
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}


# ---- the registry --------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self, definitions: list[ToolDef], gate: Gate, *, list_all: bool = False):
        """`list_all` makes `specs()` list every tool, for a runner that reads the tool list only once per session.
        The phase gate still answers `not_allowed_in_phase` at call time."""
        self.definitions = {d.name: d for d in definitions}
        self.gate = gate
        self.list_all = list_all
        self._lock = asyncio.Lock()

    def names(self) -> list[str]:
        return list(self.definitions)

    def allowed_names(self) -> list[str]:
        return [n for n in self.gate.allowed_names() if n in self.definitions]

    def specs(self) -> list[ToolSpec]:
        """The tools allowed right now, ready for a runner."""
        return [self._spec(self.definitions[n]) for n in (self.names() if self.list_all else self.allowed_names())]

    def all_specs(self) -> list[ToolSpec]:
        """Every tool, for tests and documentation."""
        return [self._spec(d) for d in self.definitions.values()]

    def _spec(self, definition: ToolDef) -> ToolSpec:
        async def handler(args: dict, _name=definition.name) -> ToolResult:
            return await self.call(_name, args)
        return ToolSpec(definition.name, definition.description, definition.input_schema, handler)

    async def call(self, name: str, args: dict | None) -> ToolResult:
        """Run a tool by name through the whole pipeline. Never raises for tool problems."""
        args = args if isinstance(args, dict) else {}
        await asyncio.sleep(0)     # a yield point, so a requested stop and the watchdog get to run between calls
        definition = self.definitions.get(name)
        if definition is None:
            return fail('unknown_tool', f'There is no tool named {name}.', allowed=self.allowed_names())
        async with self._lock:
            allowed = self.allowed_names()
            if name not in allowed:
                result = fail('not_allowed_in_phase', f'{name} cannot be used now ({self.gate.phase_label()}). Use one of the allowed tools.', allowed=allowed)
                await self.gate.after(definition, args, result)
                return result
            errors = validate(definition.input_schema, args)
            if errors:
                result = fail('invalid_arguments', '; '.join(errors[:6]), schema_hint=definition.input_schema.get('required', []))
                await self.gate.after(definition, args, result)
                return result
            refusal = await self.gate.before(definition, args)
            if refusal is not None:
                refusal.is_error = True
                await self.gate.after(definition, args, refusal)
                return refusal
            try:
                result = await definition.handler(args)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a tool failure is a result, never a crashed run
                result = fail('tool_failed', f'{name} failed: {str(exc)[:300] or type(exc).__name__}')
            result = bound(result)
            await self.gate.after(definition, args, result)
            return result
