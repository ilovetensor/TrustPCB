"""Handlers for investigations: suspects, simulations, comparisons, matching, conclusions, confirmation, summary.

All simulation work goes through the loop-prevention ledger (budgets.Ledger) before a solver
second is spent, and every result becomes evidence with its trust status attached.
"""
from __future__ import annotations

import asyncio
import re

from . import evidence as ev
from .budgets import IMPROVEMENT_DB, Experiment
from .findings import SCREEN_ONLY, FindingRejected, check_numbers, check_wording
from .handlers import OpError, validate_ops
from .lab import BadMatchingRequest, LabUnavailable, scenario_hash, scenario_key
from .tools import ToolResult, fail, ok

NOT_HELD = 'The screen improvement did not hold at confirm quality.'
UNITS = {'L': ('nH', 1e-9), 'C': ('pF', 1e-12)}      # what an agent types for a part value, and the factor to henry or farad


def _short(metrics: dict) -> dict:
    return {k: v for k, v in metrics.items() if k not in ('trust_reasons',)}


class InvestigationMixin:
    """Mixed into Handlers; `self.s` is the session and `self.store` its store."""

    # ---- opening ------------------------------------------------------------------------
    async def open_investigation(self, args: dict) -> ToolResult:
        s = self.s
        if not s.rf.get('simulatable'):
            return fail('rf_not_simulatable', s.rf.get('reason') or 'Nothing on this board can be simulated.')
        if s.budgets.investigations >= s.config.investigations:
            return fail('budget_exhausted', f'This {"review" if s.mode == "review" else "test"} allows {s.config.investigations} investigation(s) and they are used.' + (' Call advance_phase.' if s.mode == 'review' else ' Answer with what you have.'), budget='investigations')
        if any(i.status == 'open' for i in s.ledger.investigations.values()):
            return fail('investigation_open', 'Close the open investigation before opening another.')
        suspects = args['suspects']
        if len(suspects) > s.config.suspects_per_investigation:
            return fail('too_many_suspects', f'At most {s.config.suspects_per_investigation} suspects per investigation.')
        titles = [x['title'] for x in suspects]
        base_hash = scenario_hash(s.make_scenario([], None, 'screen'))
        reopened = [t for t in titles if s.ledger.reopen_check(t, base_hash)]
        if reopened:
            return fail('suspect_closed', f'"{reopened[0]}" was ruled out earlier and stays closed because the base geometry has not changed.', rule='no_reopening')
        texts = {'question': args['question'], **{f'suspects[{i}]': x['title'] for i, x in enumerate(suspects)}}
        check_wording(texts)
        investigation = self.store.create_investigation(s.review_id, args['question'])
        s.budgets.investigations += 1
        code = s.next_code('I')
        inv = s.ledger.open_investigation(investigation['id'], code)
        out = []
        for rank, item in enumerate(suspects, start=1):
            row = self.store.add_suspect(investigation['id'], item['title'], rank=rank, data={'why': item.get('why'), 'test': item.get('test')})
            st = s.ledger.add_suspect(inv, row['id'], s.next_code('S'), item['title'])
            self.persist_suspect(st)
            await self.publish_suspect(st)
            out.append({'suspect': st.code, 'title': st.title, 'rank': rank})
        await self.publish_investigation(inv, args['question'])
        return ok({'investigation': code, 'suspects': out, 'next': 'Run the unmodified base scenario first with run_simulation (no whatif_ops), then test one suspect at a time.',
                   'experiments_allowed': s.config.experiments_per_investigation})

    # ---- running ------------------------------------------------------------------------
    async def run_simulation(self, args: dict) -> ToolResult:
        s = self.s
        suspect = self.find_suspect(args['suspect_id'])
        if suspect is None:
            return fail('unknown_suspect', f'No suspect {args["suspect_id"]}. Open an investigation first.')
        try:
            ops, material = validate_ops(args.get('whatif_ops') or [], s.board, s.rf)
        except OpError as exc:
            return fail('op_not_allowed', str(exc))
        scenario = s.make_scenario(ops, material, 'screen', setup=args.get('setup'))
        kind = 'whatif' if (ops or material) else 'base'
        optimisation = bool(ops)
        label = args.get('label') or ('base' if kind == 'base' else 'what-if')
        return await self.simulate(suspect, scenario, kind, label, optimisation)

    async def gate_decision(self, suspect, inv, decision, sc_hash: str) -> ToolResult | None:
        """Turn a ledger decision into the refusal or the free cached answer; None means go ahead and run."""
        s = self.s
        if decision.close:
            await self.publish_suspect(decision.close[0])
        if decision.rule:
            s.add_limit(decision.rule, decision.message, suspect=suspect.code)
        if decision.action == 'refuse':
            if decision.close or decision.code == 'suspect_closed':
                await self.publish_investigation(inv)
            return fail(decision.code, decision.message, rule=decision.rule or None, suspect={'code': suspect.code, 'status': suspect.status})
        if decision.action == 'cached':
            s.budgets.duplicates += 1
            exp = decision.cached
            data = {'ref': exp.ref, 'cached': True, 'free': True, 'notice': decision.message, 'duplicate_count': max(0, suspect.requests.get(sc_hash, 1) - 1),
                    **s.result_view(exp)}
            if decision.close:
                data['suspect_closed'] = {'code': suspect.code, 'status': suspect.status, 'why': suspect.reason}
            return ok(data)
        return None

    async def simulate(self, suspect, scenario: dict, kind: str, label: str, optimisation: bool) -> ToolResult:
        s = self.s
        inv = s.ledger.investigations[suspect.investigation_id]
        sc_hash, key = scenario_hash(scenario), scenario_key(scenario)
        base_hash = scenario_hash(s.make_scenario([], None, 'screen'))
        decision = s.ledger.decide(suspect, sc_hash, key, kind, optimisation=optimisation, base_hash=base_hash)
        gated = await self.gate_decision(suspect, inv, decision, sc_hash)
        if gated is not None:
            return gated
        ref = s.next_ref()
        await s.emit('sim_queued', {'ref': ref, 'hash': sc_hash, 'label': label, 'suspect': suspect.code, 'quality': scenario['quality'], 'kind': kind})

        async def progress(update: dict) -> None:
            await s.emit('sim_progress', {'ref': ref, 'hash': sc_hash, **update})

        max_seconds = min(s.budgets.solver_left() or s.budgets.wall_left(), s.budgets.wall_left())
        async with s.solving():
            try:
                run = await s.lab.run_scenario(review_id=s.review_id, version_id=s.version_id, scenario=scenario, scenario_hash=sc_hash,
                                               investigation_id=inv.id, max_seconds=max_seconds, on_progress=progress)
            except LabUnavailable as exc:
                return fail('rf_unavailable', str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                run = {'status': 'failed', 'error': f'{type(exc).__name__}: {str(exc)[:200]}'}
        return await self.finish_run(suspect, inv, scenario, run, ref, sc_hash, key, kind, label, repair=decision.repair)

    async def finish_run(self, suspect, inv, scenario, run, ref, sc_hash, key, kind, label, *, repair: bool) -> ToolResult:
        s = self.s
        metrics = ev.metrics_from_result(run, s.goal)
        if run.get('status') != 'done':
            metrics.update(trust_status='fail', trust_reasons=[run.get('error') or 'The run did not finish.'])
        s.budgets.add_solver(run.get('solve_s') or metrics.get('solve_s') or 0)
        good = ev.trusted(metrics, run)
        delta = None
        if good and kind in ('whatif', 'confirm') and inv.base is not None and inv.base.ok and scenario['quality'] == inv.base.quality:
            delta = ev.delta(metrics, inv.base.metrics)
        values = ev.simulation_values(ref=ref, label=label, quality=scenario['quality'], sc_hash=sc_hash, scenario=scenario, run=run, metrics=metrics, suspect=suspect.code)
        if delta:
            values['measurements'] = {**values['measurements'], **ev.delta_measurements(delta)}
            values['compared_with'] = inv.base.ref
        row = self.store.add_evidence(s.review_id, ev.CONFIRM if scenario['quality'] == 'confirm' else ev.SIMULATION, 'run_simulation' if kind != 'confirm' else 'confirm_result', values)
        exp = Experiment(ref, sc_hash, key, suspect.id, kind, label, scenario['quality'], good, metrics, delta, False, row['id'], repair, ops=list(scenario.get('whatif_ops') or []))
        s.refs[ref] = {'exp': exp, 'scenario': scenario, 'run': run, 'evidence': row, 'label': label}
        meets = ev.meets_target(metrics, s.goal, s.rf)
        closures = s.ledger.record(suspect, exp, meets_target=meets, base_hash=scenario_hash(s.make_scenario([], None, 'screen')), repair=repair)
        if kind == 'whatif':
            s.budgets.experiments += 1
        sim_done = {'ref': ref, 'hash': sc_hash, 'label': label, 'ok': good, 'trust': values['trust'], 'quality': scenario['quality'], 'kind': kind,
                    'measurements': values['measurements'], 'delta': delta, 'meets_target': meets, 'solve_s': metrics.get('solve_s'), 'suspect': suspect.code,
                    'evidence': row['code'], 'curve': run.get('curve')}
        await s.emit('sim_done', sim_done)
        for closed, status, why in closures:
            s.add_limit({'no_effect': 'no_progress', 'could_not_test': 'repair_limit'}.get(status, status), why, suspect=closed.code)
        await self.publish_suspect(suspect)
        await self.publish_investigation(inv)
        data = {'ref': ref, 'evidence': row['code'], 'label': label, 'kind': kind, 'quality': scenario['quality'], 'trust': values['trust'], 'cached': False,
                **s.result_view(exp), 'experiments_left': max(0, s.config.experiments_per_investigation - inv.experiments)}
        notices = []
        if not good:
            repairs_left = 'one repair allowed' if (not repair and inv.repairs < 2 and suspect.status == 'open') else 'no repair left'
            notices.append(f'This run failed its trust checks and cannot support a finding ({repairs_left}). Pass setup to retry with a changed setup.')
        if meets:
            notices.append('The tuning target is met. Optimising further is not allowed: confirm the result and close the investigation.')
        elif exp.delta and ev.is_improvement(exp.delta) and kind == 'whatif':
            notices.append('This change improves the worst in-band S11 by at least 1 dB against the base. Matching can be tried on it too: tune_matching with this ref tests the combination.')
        for closed, status, why in closures:
            notices.append(f'Suspect {closed.code} is now closed as {status}: {why}')
        if notices:
            data['notices'] = notices
        data['suspect'] = {'code': suspect.code, 'status': suspect.status}
        return ok(data)

    # ---- looking at results ---------------------------------------------------------------
    def _ref(self, code: str):
        item = self.s.refs.get(code)
        if item is not None:
            return item
        for row in self.store.list_evidence(self.s.review_id):
            values = row['values'] or {}
            if values.get('ref') == code:
                return {'exp': None, 'run': values.get('run') or {'result': {}}, 'evidence': row, 'scenario': {}, 'label': values.get('label'), 'metrics': values.get('metrics')}
        return None

    async def view_result(self, args: dict) -> ToolResult:
        s = self.s
        item = self._ref(args['ref'])
        if item is None:
            return fail('unknown_result', f'No result {args["ref"]}.')
        if item.get('matching'):
            return fail('not_a_run', f'{args["ref"]} is a matching calculation, not a solver run. View the run it was applied to: {(item["exp"].source_ref if item.get("exp") else None) or "its source run"}.')
        try:
            image = await s.lab.view_result(review_id=s.review_id, result=item['run'], kind=args['kind'], params={k: v for k, v in args.items() if k not in ('ref', 'kind')})
        except LabUnavailable as exc:
            return fail('unavailable', str(exc))
        s.budgets.add_images(s.budget_key)
        return ok({'ref': args['ref'], 'kind': args['kind'], 'caption': image.get('caption'), 'images_left': s.budgets.images_left(s.budget_key)},
                  images=[{'mime': image['mime'], 'data_b64': image['data_b64']}])

    async def compare_results(self, args: dict) -> ToolResult:
        s = self.s
        a, b = self._ref(args['a']), self._ref(args['b'])
        if a is None or b is None:
            return fail('unknown_result', f'No result {args["a"] if a is None else args["b"]}.')
        va, vb = a['evidence']['values'], b['evidence']['values']
        ma, mb = va.get('metrics') or {}, vb.get('metrics') or {}
        trusted = (va.get('trust') or {}).get('status') == 'ok' and (vb.get('trust') or {}).get('status') == 'ok'
        if va.get('quality') != vb.get('quality'):
            return fail('different_quality', 'Compare runs of the same quality only; a screen run and a confirm run use different meshes.')
        delta = ev.delta(mb, ma)
        measurements = ev.delta_measurements(delta)
        extra: dict = {}
        if not a.get('matching') and not b.get('matching'):
            try:
                extra = await s.lab.compare_results(a=a['run'], b=b['run'])
                for name, item in (extra.get('measurements') or {}).items():
                    measurements.setdefault(name if name.startswith('delta_') else f'delta_{name}', item)
            except Exception:
                extra = {}
        # a dip that stays put while the antenna changes is probably a board mode
        changed_antenna = (va.get('whatif_ops') or []) != (vb.get('whatif_ops') or [])
        fixed = ev.fixed_dips(ma.get('dips') or [], mb.get('dips') or []) if changed_antenna else []
        for index, dip in enumerate(fixed, start=1):
            measurements[f'fixed_dip_{index}'] = {'value': round(dip['f_hz'] / 1e6, 1), 'unit': 'MHz'}
        if fixed:
            hint = (f'{len(fixed)} dip(s) stay where they were between {args["a"]} and {args["b"]} although the antenna changed: ' +
                    ', '.join(f'{d["f_hz"] / 1e6:.0f} MHz' for d in fixed) + '. A dip that does not move with the antenna is probably a board (ground plane) mode, not the antenna\'s own resonance.')
        elif changed_antenna:
            hint = 'No dip stays fixed between these runs, so the dips seen here move with the antenna.'
        else:
            hint = 'Both results share the same antenna geometry, so dips say nothing about a board mode.'
        values = {'kind': 'simulated', 'comparative': True, 'quality': va.get('quality'), 'trust': {'status': 'ok' if trusted else 'fail', 'reasons': [] if trusted else ['one of the runs failed its trust checks']},
                  'measurements': measurements, 'a': args['a'], 'b': args['b'], 'label': f'{args["b"]} compared with {args["a"]}'}
        row = self.store.add_evidence(s.review_id, ev.COMPARISON, 'compare_results', values)
        data = {'evidence': row['code'], 'a': args['a'], 'b': args['b'], 'trust': values['trust'], 'b_minus_a': measurements,
                'in_band': {'judged_by': 'worst S11 across the band, then the mean (as built)', 'a': s.in_band_view(ma), 'b': s.in_band_view(mb)},
                's11_dips': {'a': [{'frequency_mhz': round(d['f_hz'] / 1e6, 1), 's11_db': d['s11_db']} for d in ma.get('dips') or []],
                             'b': [{'frequency_mhz': round(d['f_hz'] / 1e6, 1), 's11_db': d['s11_db']} for d in mb.get('dips') or []]},
                'board_mode_hint': hint, 'note': extra.get('note'),
                'meaning': 'Negative S11 change means better matching. These are comparisons, not absolute results.' if values['quality'] == 'screen' else None}
        impedance = {k: s.impedance_view(m) for k, m in (('a', ma), ('b', mb)) if s.impedance_view(m)}
        if impedance:
            data['band_centre_impedance'] = impedance
        return ok(data)

    # ---- matching: a zero-solver experiment --------------------------------------------------
    @staticmethod
    def _changes_from(parts: list[dict]) -> list[dict]:
        return [{'ref': p['ref'], 'kind': p.get('kind'), 'value': p.get('value')} for p in parts]

    @staticmethod
    def _parts_text(parts: list[dict]) -> str:
        return ', '.join(f'{p["ref"]} {str(p["was"]).replace(" (fitted)", "")} to {p["now"]}' for p in parts) or 'no part changed'

    def _explicit_changes(self, parts: list[dict]) -> list[dict]:
        out = []
        for part in parts:
            kind = part['kind']
            if kind == 'none':
                out.append({'ref': part['ref'], 'kind': None, 'value': None})
                continue
            if not isinstance(part.get('value'), (int, float)) or part['value'] <= 0:
                raise OpError(f'{part["ref"]}: a {kind} part needs a positive value ({UNITS[kind][0]}).')
            out.append({'ref': part['ref'], 'kind': kind, 'value': float(part['value']) * UNITS[kind][1]})
        return out

    def _matching_metrics(self, src: dict, out: dict) -> dict:
        """The result of a matching change in the same shape as a run: the in-band numbers, dips and impedance of the matched antenna."""
        stats = out.get('predicted') or {}
        centre = out.get('zin_band_centre') or {}
        metrics = {'trust_status': src.get('trust_status'), 'trust_reasons': [], 'solve_s': 0, 'basis': 'as_built',
                   'band_worst_s11_db': stats.get('band_worst_db'), 'band_mean_s11_db': stats.get('band_mean_db'), 's11_min_db': stats.get('s11_min_db'),
                   'f_res_hz': stats.get('f_min_hz'), 'as_built_band_worst_db': stats.get('band_worst_db'), 'as_built_band_best_db': stats.get('band_best_db'),
                   'as_built_band_mean_db': stats.get('band_mean_db'), 'antenna_only_band_worst_db': src.get('antenna_only_band_worst_db'),
                   'antenna_only_band_mean_db': src.get('antenna_only_band_mean_db'), 'dmax_dbi': src.get('dmax_dbi'),
                   'first_series_resonance_hz': out.get('first_series_resonance_hz'), 'centre_f_hz': centre.get('f_hz'), 'centre_re': centre.get('re'), 'centre_im': centre.get('im'),
                   'centre_basis': 'radio_side'}
        if out.get('dips'):
            metrics['dips'] = [{'f_hz': d['f_hz'], 's11_db': d['s11_db']} for d in out['dips']][:ev.MAX_DIPS]
        return {k: v for k, v in metrics.items() if v is not None or k.startswith('trust')}

    def _part_measurements(self, parts: list[dict]) -> dict:
        out = {}
        for p in parts:
            if p.get('kind') in UNITS and p.get('value'):
                unit, factor = UNITS[p['kind']]
                out[f'{p["ref"]}_{p["position"]}'] = {'value': round(p['value'] / factor, 3), 'unit': unit}
        return out

    async def tune_matching(self, args: dict) -> ToolResult:
        """Matching parts for a stored, trusted run (a base, a what-if or both together). On a screen run of an open
        investigation this is an experiment of kind `matching`: zero solver time, cached, counted, and it can be the winner."""
        s = self.s
        item = self._ref(args['ref'])
        if item is None:
            return fail('unknown_result', f'No result {args["ref"]}.')
        if item.get('matching'):
            return fail('not_a_run', f'{args["ref"]} is already a matching calculation. Apply matching to the run it came from, or to another run.')
        values_in = item['evidence']['values']
        if (values_in.get('trust') or {}).get('status') != 'ok':
            return fail('trust_failed', 'The result failed its trust checks; matching values from it would be meaningless.')
        try:
            changes = self._explicit_changes(args['parts']) if args.get('parts') else None
        except OpError as exc:
            return fail('parts_not_allowed', str(exc))
        source = item.get('exp')
        suspect = None
        if source is not None and source.quality == 'screen' and source.kind in ('base', 'whatif'):
            suspect = self.find_suspect(args['suspect_id']) if args.get('suspect_id') else s.ledger.suspects.get(source.suspect_id)
            if args.get('suspect_id') and suspect is None:
                return fail('unknown_suspect', f'No suspect {args["suspect_id"]}.')
            if suspect is not None and s.ledger.investigations[suspect.investigation_id].status != 'open':
                suspect = None                       # the investigation is closed (phase 5): a calculation, not an experiment
        params = {'band_hz': s.rf.get('band_hz') or s.goal.get('band_hz'), 'target_db': s.goal.get('target_s11_db', -10), **({'changes': changes} if changes else {})}
        if suspect is None:
            return await self._matching_calculation(args, item, values_in, params)
        inv = s.ledger.investigations[suspect.investigation_id]
        wanted = {'matching': True, 'on': source.hash, 'parts': [[p['ref'], p['kind'], p.get('value')] for p in args['parts']] if changes else 'search', 'band': params['band_hz'], 'target': params['target_db']}     # values as typed (nH, pF): in henry they would round away
        sc_hash, key = scenario_hash(wanted), scenario_key(wanted)
        decision = s.ledger.decide(suspect, sc_hash, key, 'matching', optimisation=True, base_hash=s.base_hash())
        gated = await self.gate_decision(suspect, inv, decision, sc_hash)
        if gated is not None:
            return gated
        try:
            out = await s.lab.tune_matching(review_id=s.review_id, version_id=s.version_id, result=item['run'], params=params)
        except BadMatchingRequest as exc:
            return fail('parts_not_allowed', str(exc))
        except LabUnavailable as exc:
            inv.matching_unavailable = str(exc)          # no matching network to work with on this board: the method cannot ask for it
            return fail('unavailable', str(exc))
        parts = out.get('parts') or []
        metrics = self._matching_metrics(source.metrics, out)
        delta = ev.delta(metrics, inv.base.metrics) if inv.base is not None and inv.base.ok else None
        ref = s.next_ref()
        label = f'matching on {args["ref"]}: {self._parts_text(parts)}'
        measurements = {**ev.measurements_of(metrics), **self._part_measurements(parts), **(ev.delta_measurements(delta) if delta else {})}
        evidence_values = {'kind': 'simulated', 'ref': ref, 'label': label, 'quality': 'screen', 'trust': values_in.get('trust'), 'measurements': measurements, 'metrics': metrics,
                           'experiment': 'matching', 'applied_to': args['ref'], 'parts': parts, 'whatif_ops': source.ops, 'material': (item.get('scenario') or {}).get('material'),
                           'suspect': suspect.code, 'topology': out.get('topology'), 'solve_s': 0, 'curve': out.get('curve'), 'calculation': True,
                           'compared_with': inv.base.ref if delta and inv.base else None}
        row = self.store.add_evidence(s.review_id, ev.CALCULATION, 'tune_matching', evidence_values)
        exp = Experiment(ref, sc_hash, key, suspect.id, 'matching', label, 'screen', True, metrics, delta, False, row['id'], False,
                         source_ref=args['ref'], parts=parts, ops=list(source.ops))
        s.refs[ref] = {'exp': exp, 'scenario': item.get('scenario') or {}, 'run': item['run'], 'evidence': row, 'label': label, 'matching': True}
        meets = ev.meets_target(metrics, s.goal, s.rf)
        closures = s.ledger.record(suspect, exp, meets_target=meets, base_hash=s.base_hash(), repair=False)
        s.budgets.experiments += 1
        for closed, status, why in closures:
            s.add_limit({'no_effect': 'no_progress'}.get(status, status), why, suspect=closed.code)
        await self.publish_suspect(suspect)
        await self.publish_investigation(inv)
        data = {'ref': ref, 'evidence': row['code'], 'cached': False, 'topology': out.get('topology'), **s.result_view(exp),
                'single_part_option': out.get('best_single_change'), 'note': out.get('note'),
                'wording': 'These are values for the engineer to try in their own tool; TrustPCB changes nothing.',
                'caution': 'Computed from a screen-quality result: it compares with the base only. Confirm in phase 5 before quoting any value.',
                'experiments_left': max(0, s.config.experiments_per_investigation - inv.experiments)}
        notices = []
        if delta and ev.is_improvement(delta):
            notices.append('This matching change improves the worst in-band S11 by at least 1 dB against the base. Keep it for phase 5, where the exact parts are checked at confirm quality.')
        if meets:
            notices.append('The tuning target is met. Optimising further is not allowed: confirm the result and close the investigation.')
        for closed, status, why in closures:
            notices.append(f'Suspect {closed.code} is now closed as {status}: {why}')
        if notices:
            data['notices'] = notices
        data['suspect'] = {'code': suspect.code, 'status': suspect.status}
        return ok(data)

    async def _matching_calculation(self, args: dict, item: dict, values_in: dict, params: dict) -> ToolResult:
        """Matching on a confirm run, a stored result from an earlier conversation or outside an open investigation: a calculation, not an experiment."""
        s = self.s
        try:
            out = await s.lab.tune_matching(review_id=s.review_id, version_id=s.version_id, result=item['run'], params=params)
        except BadMatchingRequest as exc:
            return fail('parts_not_allowed', str(exc))
        except LabUnavailable as exc:
            return fail('unavailable', str(exc))
        parts = out.get('parts') or []
        metrics = self._matching_metrics(values_in.get('metrics') or {}, out)
        measurements = {**ev.measurements_of(metrics), **self._part_measurements(parts)}
        values = {'kind': 'simulated', 'quality': values_in.get('quality'), 'trust': values_in.get('trust'), 'measurements': measurements, 'metrics': metrics, 'topology': out.get('topology'),
                  'from_result': args['ref'], 'label': 'matching calculation', 'calculation': True, 'parts': parts, 'solve_s': 0}
        row = self.store.add_evidence(s.review_id, ev.CALCULATION, 'tune_matching', values)
        in_band = s.in_band_view(metrics)
        data = {'evidence': row['code'], 'topology': out.get('topology'), 'components': out.get('components'), 'parts_changed': [{'part': p['ref'], 'position': p['position'], 'was': p['was'], 'now': p['now']} for p in parts],
                'predicted': out.get('predicted'), 'in_band': in_band, 'note': out.get('note'), 'single_part_option': out.get('best_single_change'),
                'wording': 'These are values for the engineer to try in their own tool; TrustPCB changes nothing.',
                'not_an_experiment': 'This was not logged as an experiment: it was applied to a confirm-quality or stored result, or no investigation is open for it.'}
        if values_in.get('quality') == 'screen':
            data['caution'] = 'Computed from a screen-quality result: quote the values only after a confirm run.'
        return ok(data)

    # ---- concluding ---------------------------------------------------------------------
    def _suspect_rows(self, suspect, extra_codes: list[str]) -> list[dict]:
        rows = [e.evidence_id for e in suspect.experiments if e.evidence_id]
        out = [r for r in (self.store.get_evidence(i) for i in rows) if r]
        for code in extra_codes:
            row = self.s.engine.row_by_code(code)
            if row:
                out.append(row)
        return out

    async def conclude_suspect(self, args: dict) -> ToolResult:
        s = self.s
        suspect = self.find_suspect(args['suspect_id'])
        if suspect is None:
            return fail('unknown_suspect', f'No suspect {args["suspect_id"]}.')
        if suspect.closed:
            return fail('suspect_closed', f'Suspect {suspect.code} is already closed as {suspect.status} and stays closed.', rule='no_reopening')
        trusted_tests = [e for e in suspect.experiments if e.kind in ('whatif', 'matching') and e.ok]
        verdict = args['verdict']
        if verdict in ('confirmed', 'ruled_out') and not trusted_tests:
            return fail('not_supported', f'A suspect can be {verdict.replace("_", " ")} only after at least one trusted experiment (a what-if or a matching experiment). Run one, or conclude it as inconclusive.')
        if verdict == 'confirmed' and not any(ev.is_improvement(e.delta) or ev.meets_target(e.metrics, s.goal, s.rf) for e in trusted_tests):
            return fail('not_supported', 'No trusted experiment shows a clear effect (at least 1 dB better worst in-band S11 than the base), so the suspect cannot be confirmed.')
        rows = self._suspect_rows(suspect, args.get('evidence_ids') or [])
        allowed = [q for r in rows for q in ev.quantities_of(r)] + s.context()
        try:
            texts = {'reasoning': args['reasoning']}
            check_wording(texts)
            check_numbers(texts, allowed, compare_only=True)     # a screen number may appear here when the sentence says it is a screen run or a comparison
        except FindingRejected as exc:
            return fail(exc.code, exc.message, **exc.data)
        base = s.base_hash()
        s.ledger.close_suspect(suspect, verdict, args['reasoning'], base)
        suspect.reason = args['reasoning']
        await self.publish_suspect(suspect)
        inv = s.ledger.investigations[suspect.investigation_id]
        await self.publish_investigation(inv)
        remaining = [x.code for x in inv.suspects if not x.closed]
        return ok({'suspect': suspect.code, 'status': suspect.status, 'remaining_suspects': remaining})

    def closing_budget_allows_more(self, inv) -> bool:
        """Is there budget left to keep investigating? Not when a global budget ended, phase 4 calls ran out or the experiments are used."""
        s = self.s
        return not (s.budgets.global_exhausted() or s.budgets.calls_left(4) <= 0 or inv.experiments >= s.config.experiments_per_investigation)

    async def close_investigation(self, args: dict) -> ToolResult:
        s = self.s
        inv = self.find_investigation(args['investigation_id'])
        if inv is None:
            return fail('unknown_investigation', f'No investigation {args["investigation_id"]}.')
        if inv.status != 'open':
            return fail('investigation_closed', 'This investigation is already closed.')
        outcome = args['outcome']
        winner = s.ledger.winner(inv)
        if outcome == 'improved':
            if winner is None:
                return fail('not_supported', 'No trusted what-if or matching experiment improved the worst in-band S11 by at least 1 dB, so the outcome cannot be "improved".')
            finding = self.store.get_finding_by_code(s.review_id, args.get('finding_code') or '')
            if finding is None:
                return fail('finding_required', 'Record the finding first with record_finding and pass its code as finding_code.')
        else:
            if winner is not None:
                return fail('improvement_found', f'{winner.ref} ({winner.label}) improves the worst in-band S11 by {ev.gain_db(winner.delta):.1f} dB against the base, so this investigation cannot close as {outcome.replace("_", " ")}. '
                            'Record a finding for it and close as improved: phase 5 then checks it at confirm quality.', winner=winner.ref)
            gaps = s.ledger.closing_gaps(inv) if self.closing_budget_allows_more(inv) else []
            if gaps:
                return fail('investigation_incomplete', f'The investigation cannot close as {outcome.replace("_", " ")} yet while budget remains: ' + '; '.join(gaps) + '.',
                            missing=gaps, experiments_left=max(0, s.config.experiments_per_investigation - inv.experiments))
            finding = self.store.get_finding_by_code(s.review_id, args['finding_code']) if args.get('finding_code') else None
        rows = [r for x in inv.suspects for r in self._suspect_rows(x, [])]
        allowed = [q for r in rows for q in ev.quantities_of(r)] + s.context()
        try:
            check_wording({'summary': args['summary']})
            check_numbers({'summary': args['summary']}, allowed, compare_only=True)
        except FindingRejected as exc:
            return fail(exc.code, exc.message, **exc.data)
        for suspect in inv.suspects:
            if not suspect.closed:
                s.ledger.close_suspect(suspect, 'not_tested', 'The investigation closed before this suspect was tested.')
                await self.publish_suspect(suspect)
        inv.status, inv.outcome = 'closed', outcome
        if outcome == 'improved':
            s.machine.improved = True
        self.store.update_investigation(inv.id, status='closed', outcome={'outcome': outcome, 'summary': args['summary'], 'finding': finding and finding['code'],
                                                                           'winner': winner and winner.ref, 'base': inv.base and inv.base.ref,
                                                                           'winner_change': winner and {'ops': winner.ops, 'parts': winner.parts or [], 'experiment': winner.kind}})
        await self.publish_investigation(inv)
        return ok({'investigation': inv.code, 'outcome': outcome, 'winner': winner and winner.ref, 'next': 'Call advance_phase.'})

    # ---- confirming ---------------------------------------------------------------------
    async def _confirm_run(self, inv, screen: Experiment, scenario: dict, which: str, *, delta_base: Experiment | None) -> tuple[ToolResult, Experiment | None]:
        """One confirm-quality solver run of `scenario`, recorded as the confirm of `which`. Returns the tool result and the experiment (None on failure)."""
        s = self.s
        sc_hash, key = scenario_hash(scenario), scenario_key(scenario)
        suspect = next(x for x in inv.suspects if x.id == screen.suspect_id)
        cached = s.ledger.cache.get(sc_hash)
        if cached is not None and not cached.ok:
            return fail('duplicate_failed_run', 'This confirm run already failed with this setup. Change the setup or stop.', rule='duplicate_scenario'), None
        repair = key in inv.failures_by_key
        if repair and (inv.repairs_by_key.get(key, 0) >= 1 or inv.repairs >= 2):
            return fail('repair_limit', 'Repair limit reached for this confirm run.', rule='repair_limit'), None
        ref = s.next_ref()
        s.budgets.confirm_runs += 1
        await s.emit('sim_queued', {'ref': ref, 'hash': sc_hash, 'label': f'confirm {which}', 'suspect': suspect.code, 'quality': 'confirm', 'kind': 'confirm'})

        async def progress(update: dict) -> None:
            await s.emit('sim_progress', {'ref': ref, 'hash': sc_hash, **update})

        async with s.solving():
            try:
                run = await s.lab.run_scenario(review_id=s.review_id, version_id=s.version_id, scenario=scenario, scenario_hash=sc_hash, investigation_id=inv.id,
                                               max_seconds=min(s.budgets.solver_left() or s.budgets.wall_left(), s.budgets.wall_left()), on_progress=progress)
            except LabUnavailable as exc:
                return fail('rf_unavailable', str(exc)), None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                run = {'status': 'failed', 'error': f'{type(exc).__name__}: {str(exc)[:200]}'}
        # A confirm run is compared with the confirm run of the base, not the screen base.
        previous_base = inv.base
        if delta_base is not None:
            inv.base = delta_base
        try:
            result = await self.finish_run(suspect, inv, scenario, run, ref, sc_hash, key, 'confirm', f'confirm {which}', repair=repair)
        finally:
            inv.base = previous_base
        exp = s.refs[ref]['exp']
        return result, (exp if exp.ok else None)

    @staticmethod
    def _row(metrics: dict | None, ref: str | None = None) -> dict | None:
        """The numbers of the screen against confirm table for one result."""
        if not metrics:
            return None
        return {k: v for k, v in {
            'ref': ref, 'band_worst_s11_db': metrics.get('band_worst_s11_db'), 'band_mean_s11_db': metrics.get('band_mean_s11_db'),
            'deepest_dip_mhz': round(metrics['f_res_hz'] / 1e6, 1) if metrics.get('f_res_hz') is not None else None, 'deepest_dip_db': metrics.get('s11_min_db'),
            'centre_resistance_ohm': metrics.get('centre_re'), 'centre_reactance_ohm': metrics.get('centre_im'),
            'first_series_resonance_mhz': round(metrics['first_series_resonance_hz'] / 1e6, 1) if metrics.get('first_series_resonance_hz') is not None else None}.items() if v is not None}

    async def _confirm_matching(self, inv, winner: Experiment, run_exp: Experiment) -> dict:
        """Matching at confirm quality on the confirmed geometry: the screen's parts locked first, and free tuning. No solver time."""
        s = self.s
        run_item = s.refs[run_exp.ref]
        params = {'band_hz': s.rf.get('band_hz') or s.goal.get('band_hz'), 'target_db': s.goal.get('target_s11_db', -10)}
        out: dict = {'locked': None, 'free': None, 'notes': []}
        trust = {'status': 'ok', 'reasons': []}
        for mode in ('locked', 'free'):
            if mode == 'locked' and not winner.parts:
                continue
            call = dict(params, **({'changes': self._changes_from(winner.parts)} if mode == 'locked' else {}))
            try:
                got = await s.lab.tune_matching(review_id=s.review_id, version_id=s.version_id, result=run_item['run'], params=call)
            except LabUnavailable as exc:
                out['notes'].append(f'Matching at confirm quality ({mode}) could not be calculated: {exc}')
                continue
            parts = got.get('parts') or []
            metrics = self._matching_metrics(run_exp.metrics, got)
            label = 'confirm matching, parts locked' if mode == 'locked' else 'confirm matching, free tuning'
            ref = s.next_ref()
            measurements = {**ev.measurements_of(metrics), **self._part_measurements(parts)}
            values = {'kind': 'simulated', 'ref': ref, 'label': label, 'quality': 'confirm', 'trust': trust, 'measurements': measurements, 'metrics': metrics, 'experiment': 'matching',
                      'applied_to': run_exp.ref, 'parts': parts, 'whatif_ops': run_exp.ops, 'solve_s': 0, 'curve': got.get('curve'), 'calculation': True, 'mode': mode}
            row = self.store.add_evidence(s.review_id, ev.CALCULATION, 'confirm_result', values)
            exp = Experiment(ref, scenario_hash({'confirm_matching': mode, 'on': run_exp.hash, 'parts': [[p['ref'], p['now']] for p in winner.parts] if mode == 'locked' else 'search'}), '', winner.suspect_id,
                             'matching', label, 'confirm', True, metrics, None, False, row['id'], False, source_ref=run_exp.ref, parts=parts, ops=list(run_exp.ops))
            s.refs[ref] = {'exp': exp, 'scenario': run_item['scenario'], 'run': run_item['run'], 'evidence': row, 'label': label, 'matching': True}
            out[mode] = {'ref': ref, 'evidence': row['code'], 'exp': exp, 'row': row, 'parts': parts, 'metrics': metrics}
        return out

    async def confirm_result(self, args: dict) -> ToolResult:
        """Confirm-quality runs of the base and of the winner, a pair of (what-if operations, matching parts). A matching-only winner shares the base run,
        so it costs one run. The parts are then locked and re-tuned at confirm quality, and the comparison with the screen is reported honestly."""
        s = self.s
        inv = self.find_investigation(args['investigation_id'])
        if inv is None:
            return fail('unknown_investigation', f'No investigation {args["investigation_id"]}.')
        which = args['which']
        screen = inv.base if which == 'base' else s.ledger.winner(inv)
        if screen is None or not screen.ok:
            return fail('nothing_to_confirm', 'There is no trusted result to confirm.' if which == 'base' else 'No what-if or matching experiment improved the result enough to confirm.')
        if which in inv.confirms and inv.confirms[which].ok:
            exp = inv.confirms[which]
            s.budgets.duplicates += 1
            data = {'ref': exp.ref, 'cached': True, 'free': True, **s.result_view(exp)}
            if which == 'winner' and inv.confirmation:
                data['confirmation'] = inv.confirmation
            return ok(data)
        base_scenario = {**s.refs[inv.base.ref]['scenario'], 'quality': 'confirm'}
        scenario = {**s.refs[screen.ref]['scenario'], 'quality': 'confirm'}
        geometry_is_base = scenario_key(scenario) == scenario_key(base_scenario)
        confirm_base = inv.confirms.get('base')
        out_of_runs = s.budgets.confirm_runs >= s.config.confirm_runs or len(inv.confirms) >= s.config.confirm_runs_per_investigation

        if which == 'base':
            if out_of_runs:
                return fail('budget_exhausted', 'The confirm-run budget is used.', budget='confirm_runs')
            result, run_exp = await self._confirm_run(inv, screen, scenario, 'base', delta_base=None)
            if run_exp is None:
                return result
            inv.confirms['base'] = run_exp
            data = dict(result.structured or {})
            agree, difference = ev.screen_confirm_agree(screen.metrics, run_exp.metrics)
            data['screen_agreement'] = {'agree': agree, 'confirm_minus_screen': ev.delta_measurements(difference)}
            if not agree:
                data.setdefault('notices', []).append('The confirm run disagrees with the screen run. The confirm result replaces the screen result.')
                s.refs[screen.ref]['superseded_by'] = run_exp.ref
            winner = s.ledger.winner(inv)
            if winner is not None and scenario_key({**s.refs[winner.ref]['scenario'], 'quality': 'confirm'}) == scenario_key(base_scenario):
                data.setdefault('notices', []).append('The winner is a matching change on the base geometry, so this one run serves both. Call confirm_result with which=winner '
                                                      'for the matching check at confirm quality, at no extra solver time.')
            return ok(data)

        notices: list[str] = []
        data: dict = {}
        if geometry_is_base:
            if confirm_base is None:                 # a matching-only winner needs the confirmed base: one run serves both
                if out_of_runs:
                    return fail('budget_exhausted', 'The confirm-run budget is used.', budget='confirm_runs')
                result, confirm_base = await self._confirm_run(inv, inv.base, base_scenario, 'base', delta_base=None)
                if confirm_base is None:
                    return result
                inv.confirms['base'] = confirm_base
            run_exp = confirm_base
        else:
            if confirm_base is None:
                notices.append('Confirm the base too: a fix needs confirm runs of both the base and the winner.')
            if out_of_runs:
                return fail('budget_exhausted', 'The confirm-run budget is used.', budget='confirm_runs')
            result, run_exp = await self._confirm_run(inv, screen, scenario, 'winner', delta_base=confirm_base)
            if run_exp is None:
                return result
            inv.confirms['winner'] = run_exp
            data = dict(result.structured or {})
            agree, difference = ev.screen_confirm_agree(screen.metrics, run_exp.metrics)
            data['screen_agreement'] = {'agree': agree, 'confirm_minus_screen': ev.delta_measurements(difference)}
            if not agree:
                notices.append('The confirm run disagrees with the screen run. The confirm result replaces the screen result.')
                s.refs[screen.ref]['superseded_by'] = run_exp.ref

        # ---- matching at confirm quality (parts locked, then free), then the honest comparison -----------------------
        matching = await self._confirm_matching(inv, screen, run_exp)
        locked, free = matching['locked'], matching['free']
        winner_row = None
        if geometry_is_base:                         # a matching-only winner has no run of its own: its confirm result is the locked matching on the base run
            if locked is None:
                return fail('nothing_to_confirm', 'The winner is a matching change but its parts could not be applied at confirm quality.', notes=matching['notes'])
            ref = s.next_ref()
            winner_row = self.store.add_evidence(s.review_id, ev.CONFIRM, 'confirm_result', {**locked['row']['values'], 'ref': ref, 'label': 'confirm winner', 'derived_from': run_exp.ref, 'job_id': None})
            derived = Experiment(ref, scenario_hash({'confirm_winner_of': run_exp.hash, 'parts': [[p['ref'], p['now']] for p in screen.parts or []]}), '', screen.suspect_id, 'confirm', 'confirm winner',
                                 'confirm', True, locked['metrics'], ev.delta(locked['metrics'], run_exp.metrics), False, winner_row['id'], False,
                                 source_ref=run_exp.ref, parts=screen.parts, ops=list(run_exp.ops))
            s.refs[ref] = {'exp': derived, 'scenario': s.refs[run_exp.ref]['scenario'], 'run': s.refs[run_exp.ref]['run'], 'evidence': winner_row, 'label': 'confirm winner', 'matching': True}
            inv.confirms['winner'] = derived
            data = {'ref': ref, 'cached': False, **s.result_view(derived), 'solver_s': 0}
            final_metrics, final_ref = locked['metrics'], ref
        else:
            winner_row = s.refs[run_exp.ref]['evidence']
            final_metrics, final_ref = (locked['metrics'], locked['ref']) if locked else (run_exp.metrics, run_exp.ref)
        confirmation, summary_row = self._compare_with_screen(inv, screen, final_metrics, final_ref, locked, free)
        attach = [s.refs[confirm_base.ref]['evidence'] if confirm_base else None, winner_row, None if geometry_is_base else (locked and locked['row']), free and free['row'], summary_row]
        await self._attach_confirmation(inv, [r for r in attach if r], confirmation['outcome'], confirmation['statement'])
        existing = (self.store.get_investigation(inv.id) or {}).get('outcome') or {}
        self.store.update_investigation(inv.id, outcome={**existing, 'confirmation': confirmation})
        await self.publish_investigation(inv)
        data['confirmation'] = confirmation
        if summary_row:
            data['confirmation_evidence'] = summary_row['code']
        if confirmation['outcome'] != 'held':
            notices.append(confirmation['statement'] + ' Say so in the finding; the server has already listed it there.')
        data['notices'] = notices + matching['notes'] + list(data.get('notices') or [])
        if not data['notices']:
            data.pop('notices')
        return ok(data)

    def _compare_with_screen(self, inv, screen: Experiment, final_metrics: dict, final_ref: str, locked: dict | None, free: dict | None) -> tuple[dict, dict | None]:
        """The honest comparison: did the screen's improvement hold at confirm quality? Also returns the evidence row that carries it."""
        s = self.s
        confirm_base = inv.confirms.get('base')
        gain = None if confirm_base is None else round(-(ev.delta(final_metrics, confirm_base.metrics).get('s11_db') or 0.0), 3)
        free_gain = None if confirm_base is None or free is None else round(-(ev.delta(free['metrics'], confirm_base.metrics).get('s11_db') or 0.0), 3)
        held_locked = gain is not None and gain >= IMPROVEMENT_DB
        held_free = free_gain is not None and free_gain >= IMPROVEMENT_DB
        agree, _ = ev.screen_confirm_agree(screen.metrics, final_metrics)
        meets = ev.meets_target(final_metrics, s.goal, s.rf)
        if confirm_base is None:
            outcome, statement = 'unconfirmed', 'The base was not confirmed, so the gain over the base cannot be judged at confirm quality.'
        elif held_locked and agree:
            outcome, statement = 'held', 'The improvement held at confirm quality.'
        elif held_locked:
            outcome = 'held_smaller'
            statement = ('The improvement held at confirm quality but the screen overstated it; the confirm numbers replace the screen numbers.' +
                         ('' if meets else ' The tuning target is still not met at confirm quality.'))
        elif held_free:
            outcome, statement = 'held_after_retune', NOT_HELD[:-1] + ' with the screen\'s parts; re-tuning the parts at confirm quality still improves the worst in-band S11.'
        else:
            outcome, statement = 'not_held', NOT_HELD
        part_view = lambda parts: [{k: p.get(k) for k in ('ref', 'position', 'was', 'now')} for p in parts or []]
        confirmation = {
            'outcome': outcome, 'statement': statement, 'held': held_locked, 'agree_with_screen': agree,
            'winner': {'ref': screen.ref, 'ops': screen.ops, 'parts': part_view(screen.parts), 'experiment': screen.kind},
            'rows': {'base': {'screen': self._row(inv.base.metrics, inv.base.ref), 'confirm': self._row(confirm_base.metrics if confirm_base else None, confirm_base.ref if confirm_base else None)},
                     'winner': {'screen': self._row(screen.metrics, screen.ref), 'confirm': self._row(final_metrics, final_ref)}},
            'matching_at_confirm': {m: {'ref': x['ref'], 'parts': part_view(x['parts']), **(self._row(x['metrics']) or {}), 'meets_target': ev.meets_target(x['metrics'], s.goal, s.rf)}
                                    for m, x in (('locked', locked), ('free', free)) if x},
            'confirm_gain_db': gain, 'free_gain_db': free_gain, 'meets_target': meets,
        }
        inv.confirmation = confirmation
        if confirm_base is None:
            return confirmation, None
        numbers = {'confirm_base_band_worst': confirm_base.metrics.get('band_worst_s11_db'), 'confirm_winner_band_worst': final_metrics.get('band_worst_s11_db'),
                   'confirm_base_band_mean': confirm_base.metrics.get('band_mean_s11_db'), 'confirm_winner_band_mean': final_metrics.get('band_mean_s11_db')}
        measurements = {k: {'value': round(v, 2), 'unit': 'dB'} for k, v in numbers.items() if v is not None}
        if gain is not None:
            measurements['confirm_gain'] = {'value': gain, 'unit': 'dB'}
        if free is not None and free_gain is not None:
            measurements['confirm_free_tuning_band_worst'] = {'value': round(free['metrics']['band_worst_s11_db'], 2), 'unit': 'dB'}
            measurements['confirm_free_tuning_gain'] = {'value': free_gain, 'unit': 'dB'}
        values = {'kind': 'simulated', 'quality': 'confirm', 'trust': {'status': 'ok', 'reasons': []}, 'measurements': measurements, 'label': 'confirm comparison', 'outcome': outcome,
                  'statement': statement, 'confirm_held': outcome == 'held', 'calculation': True, 'confirmation': confirmation}
        return confirmation, self.store.add_evidence(s.review_id, ev.CALCULATION, 'confirm_result', values)

    async def _attach_confirmation(self, inv, rows: list[dict], outcome: str, statement: str) -> None:
        """Put the confirm evidence on the finding the investigation was closed with, and record in it when the screen improvement did not hold."""
        s = self.s
        code = ((self.store.get_investigation(inv.id) or {}).get('outcome') or {}).get('finding')
        finding = self.store.get_finding_by_code(s.review_id, code) if code else None
        if finding is None:
            return
        ids = list(dict.fromkeys(list(finding['evidence_ids'] or []) + [r['id'] for r in rows]))
        not_tested = [t for t in (finding['not_tested'] or []) if t != SCREEN_ONLY]
        if outcome in ('not_held', 'held_after_retune', 'held_smaller') and statement not in not_tested:
            not_tested.append(statement)
        updated = self.store.update_finding(finding['id'], 'review', 'confirm result attached', evidence_ids=ids, not_tested=not_tested)
        await self.publish_finding(updated)

    # ---- the end ------------------------------------------------------------------------
    async def submit_summary(self, args: dict) -> ToolResult:
        s = self.s
        allowed = [q for row in s.engine.evidence_rows() for q in ev.quantities_of(row)] + s.context()
        texts = {'summary': args['summary'], **{f'top_actions[{i}]': a for i, a in enumerate(args['top_actions'])}}
        unknown = [c for text in texts.values() for c in re.findall(r'\bF-\d{3}\b', text) if self.store.get_finding_by_code(s.review_id, c) is None]
        try:
            check_wording(texts)
            check_numbers(texts, allowed)
            if unknown:
                raise FindingRejected('unknown_finding', f'{unknown[0]} is not a finding in this review.')
        except FindingRejected as exc:
            s.summary_failures += 1
            extra = {'tries_left': max(0, 3 - s.summary_failures)}
            if s.summary_failures >= 3:
                s.mark_finished()
                return fail(exc.code, exc.message + ' The server will write the summary from the stored findings.', **exc.data, **extra)
            return fail(exc.code, exc.message, **exc.data, **extra)
        s.summary = {'text': args['summary'].strip(), 'top_actions': [a.strip() for a in args['top_actions']], 'source': 'agent'}
        s.mark_finished()
        return ok({'accepted': True, 'next': 'The review is complete. Stop here.'})
