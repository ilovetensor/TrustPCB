"""The report's section for each board simulation (power, thermal, signal): key numbers, the assumptions table, overlay stills and what was not tested.

`section_data` builds the frozen part of the snapshot from stored evidence. The stills are drawn when the report is rendered, as inline images
(an SVG of the board with the overlay raster as a data URI), so the HTML opens anywhere and the PDF is printed under the locked-down browser
that allows images from `data:` only. A report made before these simulations existed has no such section and renders as it did.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import struct
import zlib
from pathlib import Path
from typing import Any

from . import evidence as ev
from . import extent as ext
from . import simmetrics as sm

logger = logging.getLogger('trustpcb')

NOT_TESTED = {
    'power': 'Real load currents and their peaks, regulator behaviour under transients, connector and solder joint resistance, and copper thickness tolerance were not tested.',
    'thermal': 'Airflow, the enclosure, neighbouring heat sources, solder voids and part-level thermal models were not tested.',
    'signal': 'Crosstalk beyond the nets analysed, real driver edge rates, connectors and cables, and a built board were not measured.',
}
SOURCE_LABEL = {'default': 'Default (not from your design)', 'user': 'Given by the engineer', 'datasheet': 'Datasheet', 'board': 'Read from the board'}
MAX_STILLS_PER_KIND = 4
RAMP = [(0.0, (22, 14, 70)), (0.25, (84, 24, 130)), (0.5, (190, 55, 100)), (0.75, (245, 130, 40)), (1.0, (252, 230, 100))]       # dark to light, no blue at the top
REVERSE = [(0.0, (60, 120, 200)), (0.5, (245, 245, 245)), (1.0, (200, 60, 50))]


def esc(value: Any) -> str:
    return html.escape('' if value is None else str(value), quote=True)


def _trusted(row: dict) -> bool:
    return ((row.get('values') or {}).get('trust') or {}).get('status') == 'ok'


def _run_view(row: dict | None) -> dict | None:
    if not row:
        return None
    v = row['values']
    return {'code': row['code'], 'ref': v.get('ref'), 'label': v.get('label'), 'quality': v.get('quality'), 'trust': v.get('trust'), 'run': v.get('run_hash'), 'whatif_ops': v.get('whatif_ops') or [],
            'measurements': {k: m for k, m in (v.get('measurements') or {}).items() if not k.startswith('delta_')}, 'primary': v.get('primary'), 'metrics': v.get('metrics') or {},
            'overlays': [o.get('id') for o in v.get('overlays') or []], 'notes': v.get('notes') or [], 'cached': v.get('cached'), 'solve_s': v.get('solve_s')}


def section_data(rows: list[dict], summary: dict, review: dict) -> dict:
    """The snapshot's `sims`: the extent line and, for every board simulation chosen or run, what it showed and how far to trust it."""
    info = summary.get('extent') or {}
    chosen = review.get('extent')
    skipped = info.get('could_not_run') or {}
    kinds_with_runs = [k for k in ext.SIM_KINDS if any(r['type'] == ev.BOARD_SIM and (r['values'] or {}).get('sim_kind') == k for r in rows)]
    kinds = [k for k in ext.SIM_KINDS if k in (chosen or []) or k in kinds_with_runs]
    out = []
    for kind in kinds:
        mine = [r for r in rows if r['type'] == ev.BOARD_SIM and (r['values'] or {}).get('sim_kind') == kind]
        runs = [r for r in mine if not r['values'].get('comparison')]
        comparisons = [r for r in mine if r['values'].get('comparison') and r['values'].get('confirmation') and _trusted(r)]
        good = [r for r in runs if _trusted(r)]
        confirmed = [r for r in good if r['values'].get('quality') == 'confirm']
        base_confirm = next((r for r in confirmed if r['values'].get('label') == 'confirm base'), None) or next((r for r in confirmed if not r['values'].get('whatif_ops')), None)
        winner_confirm = next((r for r in confirmed if r['values'].get('label') == 'confirm winner'), None)
        base_screen = next((r for r in good if not r['values'].get('whatif_ops') and r['values'].get('quality') == 'screen'), None)
        shown_base = base_confirm or base_screen
        status = 'tested' if confirmed else ('screen_only' if good else 'not_tested')
        reason = None
        if status == 'not_tested':
            failed = [r for r in runs if not _trusted(r)]
            reason = (skipped.get(kind) or (f'{len(failed)} run(s) failed their checks and cannot support a statement: ' + '; '.join((failed[0]['values'].get('trust') or {}).get('reasons') or [])[:200] if failed else None)
                      or 'The agent did not run it in this review.')
        elif status == 'screen_only':
            reason = 'Only screen-quality runs exist. They compare changes with the base and give no absolute result.'
        used = [r for r in (base_confirm, winner_confirm, base_screen) if r] or good[:1]
        seen: set = set()
        assumptions = []
        for r in used:
            for a in r['values'].get('assumptions') or []:
                key = (a.get('name'), a.get('value'))
                if key not in seen:
                    seen.add(key)
                    assumptions.append({k: a.get(k) for k in ('name', 'label', 'value', 'unit', 'source', 'cite')})
        defaults = [a for a in assumptions if a.get('source') == 'default']
        notes = list(dict.fromkeys(n for r in used for n in (r['values'].get('notes') or [])))[:6]
        not_tested = [NOT_TESTED[kind]]
        if defaults:
            not_tested.append('Inputs nobody gave were left at defaults: ' + ', '.join(sm.assumption_text(a) for a in defaults) + '. Every number above is conditional on them.')
        if status != 'tested':
            not_tested.append('No confirm-quality (finer mesh) run exists, so no number here is final.' if status == 'screen_only' else 'No usable run exists for this simulation.')
        out.append({'kind': kind, 'label': ext.LABELS[kind], 'status': status, 'reason': reason, 'runs': len(runs), 'cached_runs': sum(1 for r in runs if r['values'].get('cached')),
                    'base': _run_view(shown_base), 'winner': _run_view(winner_confirm), 'comparison': (comparisons[-1]['values'].get('confirmation') | {'evidence': comparisons[-1]['code']}) if comparisons else None,
                    'assumptions': assumptions, 'defaults': [a['name'] for a in defaults], 'notes': notes, 'not_tested': not_tested})
    return {'method': info.get('method'), 'chosen': chosen, 'kinds': out}


# ---- html -----------------------------------------------------------------------------------------------------------------

def _value(item: dict) -> str:
    value = item.get('value')
    return f'{value:g} {item.get("unit", "")}'.strip() if isinstance(value, (int, float)) else f'{value} {item.get("unit", "")}'.strip()


def render(sims: dict | None, images: dict | None, pill, metric_name) -> str:
    """The report's section. `pill` and `metric_name` come from report.py (they are shared with the rest of the page)."""
    if not sims or not sims.get('kinds'):
        return ''
    images = images or {}
    out = ['<h2>Board simulations</h2>',
           '<p class="muted small">Simulated or estimated, never measured. Every input is listed with where it came from; an input nobody gave is a labelled default and makes the numbers conditional.</p>']
    for item in sims['kinds']:
        tone = {'tested': 'cls-simulated', 'screen_only': 'sev-medium', 'not_tested': 'cls-not_tested'}[item['status']]
        out.append(f'<div class="card"><h3>{esc(item["label"])}</h3><p>{pill(tone, {"tested": "Simulated", "screen_only": "Screen only", "not_tested": "Not tested"}[item["status"]])} '
                   f'{esc(item["reason"] or "")} <span class="muted small">{esc(item["runs"])} run(s), {esc(item["cached_runs"])} served from a stored identical run.</span></p>')
        for key, title in (('base', 'Base'), ('winner', 'Best change')):
            run = item.get(key)
            if not run:
                continue
            what = f' - what-if: {", ".join(esc(json.dumps(o)) for o in run["whatif_ops"])}' if run.get('whatif_ops') else ''
            out.append(f'<h4>{title} ({esc(run["code"])}, {esc(run["quality"])}){what}</h4><table class="metrics">' + ''.join(
                f'<tr><td>{esc(metric_name(k))}</td><td class="n">{esc(_value(m))}</td></tr>' for k, m in run['measurements'].items()) + '</table>')
            for oid in run['overlays'][:MAX_STILLS_PER_KIND]:
                still = images.get(f'sim_{item["kind"]}_{key}_{oid}')
                if still:
                    out.append(still)
        comparison = item.get('comparison')
        if comparison:
            out.append(f'<p>{pill({"held": "cls-measured", "held_smaller": "sev-medium", "not_held": "sev-high"}.get(comparison.get("outcome"), ""), str(comparison.get("outcome", "")).replace("_", " "))} '
                       f'{esc(comparison.get("statement"))} {esc(comparison.get("headline_text"))} <code>{esc(comparison.get("evidence"))}</code></p>')
        if item['assumptions']:
            out.append('<table><tr><th>Input</th><th>Value</th><th>Source</th></tr>' + ''.join(
                f'<tr><td>{esc(sm.assumption_label(a))}</td><td class="n">{esc(_value(a))}</td><td class="small">{pill("sev-medium" if a.get("source") == "default" else "", SOURCE_LABEL.get(a.get("source"), a.get("source")))}'
                f'{" " + esc(a["cite"]) if a.get("cite") else ""}</td></tr>' for a in item['assumptions']) + '</table>')
        for note in item.get('notes') or []:
            out.append(f'<p class="small muted">{esc(note)}</p>')
        out.append('<p class="small"><strong>Not tested.</strong> ' + esc(' '.join(item['not_tested'])) + '</p></div>')
    return ''.join(out)


# ---- stills -----------------------------------------------------------------------------------------------------------------

def _ramp(t, stops):
    import numpy as np
    xs = [s[0] for s in stops]
    return np.stack([np.interp(t, xs, [s[1][c] for s in stops]) for c in range(3)], axis=-1)


def overlay_png(path: Path, meta: dict) -> bytes:
    """The overlay raster as an RGBA PNG: float32 little-endian [ny, nx], NaN transparent, scaled between vmin and vmax."""
    import numpy as np
    nx, ny = int(meta['nx']), int(meta['ny'])
    data = np.fromfile(path, dtype='<f4')
    if data.size != nx * ny:
        raise ValueError(f'overlay {meta.get("id")} has {data.size} values, expected {nx * ny}')
    data = data.reshape(ny, nx).astype('float64')
    vmin, vmax = float(meta.get('vmin', 0.0)), float(meta.get('vmax', 1.0))
    t = np.clip((data - vmin) / ((vmax - vmin) or 1.0), 0.0, 1.0)
    rgb = _ramp(t, REVERSE if meta.get('scale') == 'diverging' else RAMP).astype('uint8')
    alpha = np.where(np.isnan(data), 0, 255).astype('uint8')
    rgba = np.dstack([rgb, alpha])
    raw = b''.join(b'\x00' + rgba[row].tobytes() for row in range(ny))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', nx, ny, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b'')


def still_svg(board_svg: str, png: bytes, meta: dict) -> str:
    """The board drawing with the overlay raster laid over it in board millimetres, as one inline SVG."""
    uri = base64.b64encode(png).decode()
    width, height = float(meta['nx']) * float(meta['dx']), float(meta['ny']) * float(meta['dy'])
    image = (f'<image x="{float(meta["x0"]):g}" y="{float(meta["y0"]):g}" width="{width:g}" height="{height:g}" preserveAspectRatio="none" opacity="0.8" '
             f'style="image-rendering:pixelated" href="data:image/png;base64,{uri}"/>')
    head, _, tail = board_svg.rpartition('</svg>')
    figure = head + image + '</svg>' + tail
    return figure if 'class="board"' in figure[:300] else figure.replace('<svg ', '<svg class="board" ', 1)


def legend(meta: dict) -> str:
    stops = ', '.join(f'rgb({r},{g},{b}) {int(t * 100)}%' for t, (r, g, b) in (REVERSE if meta.get('scale') == 'diverging' else RAMP))
    unit = meta.get('unit') or ''
    low, high = f'{float(meta.get("vmin", 0)):g} {unit}', f'{float(meta.get("vmax", 1)):g} {unit}'
    return (f'<div class="small" style="display:flex;gap:8px;align-items:center;margin:2px 0 8px"><span>{esc(low)}</span>'
            f'<span style="flex:0 0 160px;height:10px;border-radius:5px;background:linear-gradient(90deg,{stops})"></span><span>{esc(high)}</span>'
            f'<span class="muted">{esc(meta.get("label") or "")} ({esc(meta.get("layer") or "board")})</span></div>')


async def stills(store, review: dict, snap: dict) -> dict:
    """Overlay stills for the shown base and best change of every board simulation, drawn now from the stored rasters. A raster that cannot be drawn is simply missing."""
    from ..pcb import render_svg
    from . import service
    from .sims import board_run_folder, overlay_file, _result_of
    sims = snap.get('sims')
    if not sims or not any(k.get('base') or k.get('winner') for k in sims.get('kinds') or []):
        return {}
    try:
        board = await service.get_lab(store).load_board(version_id=review['version_id'])
    except Exception:
        logger.warning('Report for review %s has no simulation stills: the board could not be loaded', review.get('id'), exc_info=True)
        return {}

    def draw() -> dict:
        images: dict[str, str] = {}
        cache: dict[str, str] = {}
        for item in sims['kinds']:
            for key in ('base', 'winner'):
                run = item.get(key)
                folder = board_run_folder(store, review['id'], run['run']) if run and run.get('run') else None
                if folder is None:
                    continue
                metas = {m.get('id'): m for m in _result_of(folder).get('overlays') or [] if isinstance(m, dict)}
                for oid in run['overlays'][:MAX_STILLS_PER_KIND]:
                    meta, path = metas.get(oid), overlay_file(folder, oid)
                    if not meta or path is None:
                        continue
                    try:
                        view = 'bottom' if str(meta.get('layer') or '').startswith('B.') else 'top'
                        if view not in cache:
                            cache[view] = render_svg.layer_svg(board, view, render_svg.RenderOptions(theme='light', px_per_mm=8))
                        figure = still_svg(cache[view], overlay_png(path, meta), meta)
                        images[f'sim_{item["kind"]}_{key}_{oid}'] = f'<div class="card keep">{figure}{legend(meta)}<p class="muted small">{esc(item["label"])}, {esc(run["label"])} ({esc(run["code"])}): simulated.</p></div>'
                    except Exception:
                        logger.warning('Report for review %s: the %s overlay %s could not be drawn', review.get('id'), item['kind'], oid, exc_info=True)
        return images

    try:
        return await asyncio.to_thread(draw)
    except Exception:
        logger.warning('Report for review %s has no simulation stills: drawing failed', review.get('id'), exc_info=True)
        return {}
