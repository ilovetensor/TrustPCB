"""Prompt text for the review agent. Plain Markdown files, loaded by name."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent


def _read(path: Path, **values: object) -> str:
    text = path.read_text(encoding='utf-8')
    for key, value in values.items():
        text = text.replace('{' + key + '}', str(value))
    return text.strip()


def system_prompt(**values: object) -> str:
    return _read(ROOT / 'system.md', **values)


def phase_brief(phase: int, **values: object) -> str:
    return _read(ROOT / 'phases' / f'{phase}.md', **values)


def followup_prompt(**values: object) -> str:
    return _read(ROOT / 'followup.md', **values)
