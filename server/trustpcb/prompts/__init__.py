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
    """The brief of a phase. The antenna parts are left out when the antenna was not chosen; the board-simulation parts (`sim_kinds` not empty) and the inputs part
    (phase 2, when simulations or datasheets were chosen) are added. Without these keys the brief is the antenna's, as it always was."""
    antenna = values.get('antenna', True)
    kinds = list(values.get('sim_kinds') or [])
    parts: list[str] = []
    if antenna or not kinds or phase in (2, 3, 6):
        parts.append(_read(ROOT / 'phases' / f'{phase}.md', **values))
    if phase == 2 and (kinds or values.get('datasheets')):
        parts.append(_read(ROOT / 'phases' / '2_inputs.md', **values))
    if kinds and phase in (4, 5, 6):
        parts.append(_read(ROOT / 'phases' / f'{phase}_sims.md', **values))
    return '\n\n'.join(parts)


def followup_prompt(**values: object) -> str:
    return _read(ROOT / 'followup.md', **values)


def recheck_brief(**values: object) -> str:
    """The recheck briefing. For a review without the antenna the antenna paragraphs are left out; `sim_changes` adds what the previous version's simulations showed."""
    text = _read(ROOT / 'recheck.md', **values)
    if values.get('antenna') is False:
        text = text.split('What the summary and the antenna finding must say:')[0].rstrip()
        text += '\n\nWhat the summary must say: start with what changed since version {previous_version_number}, then the counts for the findings (fixed, improved, still present, worse and new).'.replace('{previous_version_number}', str(values.get('previous_version_number')))
    if values.get('sim_changes'):
        text += '\n\n' + str(values['sim_changes'])
    return text
