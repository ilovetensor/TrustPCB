"""Handlers for investigations: suspects, simulations, comparisons, matching, conclusions, confirmation, summary.

All simulation work goes through the loop-prevention ledger (budgets.Ledger) before a solver
second is spent, and every result becomes evidence with its trust status attached.
"""
from __future__ import annotations

import asyncio
import re

from . import evidence as ev
from .budgets import IMPROVEMENT_DB, Experiment
from .findings import FindingRejected, check_numbers, check_wording
from .handlers import OpError, validate_ops
from .lab import LabUnavailable, scenario_hash, scenario_key
from .tools import ToolResult, fail, ok


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

    async def simulate(self, suspect, scenario: dict, kind: str, label: str, optimisation: bool) -> ToolResult:
        s = self.s
        inv = s.ledger.investigations[suspect.investigation_id]
        sc_hash, key = scenario_hash(scenario), scenario_key(scenario)
        base_hash = scenario_hash(s.make_scenario([], None, 'screen'))
        decision = s.ledger.decide(suspect, sc_hash, key, kind, optimisation=optimisation, base_hash=base_hash)
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
        exp = Experiment(ref, sc_hash, key, suspect.id, kind, label, scenario['quality'], good, metrics, delta, False, row['id'], repair)
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
        try:
            extra = await s.lab.compare_results(a=a['run'], b=b['run'])
            for name, item in (extra.get('measurements') or {}).items():
                measurements.setdefault(name if name.startswith('delta_') else f'delta_{name}', item)
        except Exception:
            extra = {}
        values = {'kind': 'simulated', 'comparative': True, 'quality': va.get('quality'), 'trust': {'status': 'ok' if trusted else 'fail', 'reasons': [] if trusted else ['one of the runs failed its trust checks']},
                  'measurements': measurements, 'a': args['a'], 'b': args['b'], 'label': f'{args["b"]} compared with {args["a"]}'}
        row = self.store.add_evidence(s.review_id, ev.COMPARISON, 'compare_results', values)
        return ok({'evidence': row['code'], 'a': args['a'], 'b': args['b'], 'trust': values['trust'], 'b_minus_a': measurements, 'note': extra.get('note'),
                   'meaning': 'Negative S11 change means better matching. These are comparisons, not absolute results.' if values['quality'] == 'screen' else None})

    async def tune_matching(self, args: dict) -> ToolResult:
        s = self.s
        item = self._ref(args['ref'])
        if item is None:
            return fail('unknown_result', f'No result {args["ref"]}.')
        values_in = item['evidence']['values']
        if (values_in.get('trust') or {}).get('status') != 'ok':
            return fail('trust_failed', 'The result failed its trust checks; matching values from it would be meaningless.')
        try:
            out = await s.lab.tune_matching(review_id=s.review_id, version_id=s.version_id, result=item['run'],
                                            params={k: v for k, v in args.items() if k != 'ref'} | {'band_hz': s.rf.get('band_hz') or s.goal.get('band_hz'), 'target_db': s.goal.get('target_s11_db', -10)})
        except LabUnavailable as exc:
            return fail('unavailable', str(exc))
        measurements = {}
        for index, comp in enumerate(out.get('components') or []):
            measurements[f'{comp.get("name") or comp.get("kind") or "part"}_{index}'] = {'value': comp['value'], 'unit': comp['unit']}
        for name, value in (out.get('predicted') or {}).items():
            if isinstance(value, (int, float)):
                measurements[f'predicted_{name}'] = {'value': value, 'unit': 'dB' if name.endswith('_db') else 'MHz'}
        values = {'kind': 'simulated', 'quality': values_in.get('quality'), 'trust': values_in.get('trust'), 'measurements': measurements, 'topology': out.get('topology'),
                  'from_result': args['ref'], 'label': 'matching calculation', 'calculation': True}
        row = self.store.add_evidence(s.review_id, ev.CALCULATION, 'tune_matching', values)
        data = {'evidence': row['code'], 'topology': out.get('topology'), 'components': out.get('components'), 'predicted': out.get('predicted'), 'note': out.get('note'),
                'wording': 'These are values for the engineer to try in their own tool; TrustPCB changes nothing.'}
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
        trusted_tests = [e for e in suspect.experiments if e.kind == 'whatif' and e.ok]
        verdict = args['verdict']
        if verdict in ('confirmed', 'ruled_out') and not trusted_tests:
            return fail('not_supported', f'A suspect can be {verdict.replace("_", " ")} only after at least one trusted experiment. Run one, or conclude it as inconclusive.')
        if verdict == 'confirmed' and not any((e.delta or {}).get('s11_db', 0) <= -IMPROVEMENT_DB or ev.meets_target(e.metrics, s.goal, s.rf) for e in trusted_tests):
            return fail('not_supported', 'No trusted experiment shows a clear effect, so the suspect cannot be confirmed.')
        rows = self._suspect_rows(suspect, args.get('evidence_ids') or [])
        allowed = [q for r in rows for q in ev.quantities_of(r)] + s.context()
        try:
            texts = {'reasoning': args['reasoning']}
            check_wording(texts)
            check_numbers(texts, allowed)
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
                return fail('not_supported', 'No trusted experiment improved the result by at least 1 dB, so the outcome cannot be "improved".')
            finding = self.store.get_finding_by_code(s.review_id, args.get('finding_code') or '')
            if finding is None:
                return fail('finding_required', 'Record the finding first with record_finding and pass its code as finding_code.')
        else:
            finding = self.store.get_finding_by_code(s.review_id, args['finding_code']) if args.get('finding_code') else None
        rows = [r for x in inv.suspects for r in self._suspect_rows(x, [])]
        allowed = [q for r in rows for q in ev.quantities_of(r)] + s.context()
        try:
            check_wording({'summary': args['summary']})
            check_numbers({'summary': args['summary']}, allowed)
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
                                                                           'winner': winner and winner.ref, 'base': inv.base and inv.base.ref})
        await self.publish_investigation(inv)
        return ok({'investigation': inv.code, 'outcome': outcome, 'winner': winner and winner.ref, 'next': 'Call advance_phase.'})

    # ---- confirming ---------------------------------------------------------------------
    async def confirm_result(self, args: dict) -> ToolResult:
        s = self.s
        inv = self.find_investigation(args['investigation_id'])
        if inv is None:
            return fail('unknown_investigation', f'No investigation {args["investigation_id"]}.')
        which = args['which']
        screen = inv.base if which == 'base' else s.ledger.winner(inv)
        if screen is None or not screen.ok:
            return fail('nothing_to_confirm', 'There is no trusted result to confirm.' if which == 'base' else 'No what-if improved the result enough to confirm.')
        if which in inv.confirms and inv.confirms[which].ok:
            exp = inv.confirms[which]
            s.budgets.duplicates += 1
            return ok({'ref': exp.ref, 'cached': True, 'free': True, **s.result_view(exp)})
        if s.budgets.confirm_runs >= s.config.confirm_runs or len(inv.confirms) >= s.config.confirm_runs_per_investigation:
            return fail('budget_exhausted', 'The confirm-run budget is used.', budget='confirm_runs')
        source = s.refs[screen.ref]['scenario']
        scenario = {**source, 'quality': 'confirm'}
        sc_hash, key = scenario_hash(scenario), scenario_key(scenario)
        suspect = next(x for x in inv.suspects if x.id == screen.suspect_id)
        cached = s.ledger.cache.get(sc_hash)
        if cached is not None and not cached.ok:
            return fail('duplicate_failed_run', 'This confirm run already failed with this setup. Change the setup or stop.', rule='duplicate_scenario')
        repair = key in inv.failures_by_key
        if repair and (inv.repairs_by_key.get(key, 0) >= 1 or inv.repairs >= 2):
            return fail('repair_limit', 'Repair limit reached for this confirm run.', rule='repair_limit')
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
                return fail('rf_unavailable', str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                run = {'status': 'failed', 'error': f'{type(exc).__name__}: {str(exc)[:200]}'}
        # A confirm run is compared with the confirm run of the base, not the screen base.
        previous_base = inv.base
        confirm_base = inv.confirms.get('base')
        if which == 'winner' and confirm_base is not None:
            inv.base = confirm_base
        try:
            result = await self.finish_run(suspect, inv, scenario, run, ref, sc_hash, key, 'confirm', f'confirm {which}', repair=repair)
        finally:
            inv.base = previous_base
        exp = s.refs[ref]['exp']
        if exp.ok:
            inv.confirms[which] = exp
            agree, difference = ev.screen_confirm_agree(screen.metrics, exp.metrics)
            data = dict(result.structured or {})
            data['screen_agreement'] = {'agree': agree, 'confirm_minus_screen': ev.delta_measurements(difference)}
            if not agree:
                data.setdefault('notices', []).append('The confirm run disagrees with the screen run. The confirm result replaces the screen result.')
                s.refs[screen.ref]['superseded_by'] = ref
            if which == 'winner' and confirm_base is None:
                data.setdefault('notices', []).append('Confirm the base too: a fix needs confirm runs of both the base and the winner.')
            if which == 'winner' and confirm_base is not None:
                data['confirmed_improvement'] = (exp.delta or {}).get('s11_db', 0) <= -IMPROVEMENT_DB
            return ok(data)
        return result

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
