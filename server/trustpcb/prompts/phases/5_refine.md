# Phase 5, refine round

The confirm run missed the target. You now have one bounded second attempt on the same investigation. The phase is still 5.

What you may do, and the limits the server enforces:
1. Read the confirm evidence first: the confirm base, the confirm of the first winner, and the matching re-tuned at confirm quality (parts locked, and free). Note what the screen overstated and what the confirm numbers support. Screen results are compared with the base, never quoted as absolute values.
2. `run_simulation` for up to {refine_screens} screen what-ifs. Each needs `whatif_ops` (a geometry change, for example `adjust_antenna_length`) and a `reason` of at least 30 characters that cites confirm evidence by its code, with numbers copied from that evidence. The screen compares with the base. The server puts the run on the round's own suspect.
3. `tune_matching` on a refine what-if (up to {refine_matching} matching experiments, no solver time). A geometry change together with matching is the candidate the first attempt did not try.
4. `confirm_result` with `which` set to `refine` once, for the new best candidate: the what-if operations and matching parts together. Both the base and the first winner are already confirmed.
5. `record_finding` with `update` for the finding of this investigation, then `advance_phase` to end the round. The report always runs after it.

Rules: the stop rules still apply (a duplicate request returns the stored result; two tests in a row that change nothing close the suspect; meeting the target ends optimising). A budget wall ends the round. Leaving early is refused while solver budget allows and the round has not tried a what-if, matching on it, and a confirm of the best candidate.

If the refine candidate meets the target at confirm quality, the finding states the combined fix as tested in simulation, for the engineer to apply in their own tool. If it does not, the finding states both attempts honestly.
