## Board simulations: {sim_text}

The same method as the antenna, for each kind chosen: `open_investigation` with `kind` and up to three ranked suspects (open one investigation per kind; this review allows {investigations} in all), then:
1. `get_sim_inputs`, then `run_board_sim` with no `whatif`: the base. Everything is compared with it. A base run that used defaults says so in `defaults_in_use`.
2. One suspect at a time, one change per run: `run_board_sim` with a `whatif` (its ops are in `accepted_whatif_ops`). A what-if lives only in the simulation; the server edits nothing.
3. Read the result before the next test: `primary` first, then every number, then the inputs with their sources. `compare_sims` gives the difference between two runs of the same kind.
4. A change that makes the primary metric 10 % better or more is an improvement. Two runs in a row that move it by under 2 % close the suspect. An identical request returns the stored result and counts as a repeat. You have {sim_runs} new runs in all; a stored identical run is free.
5. Screen runs only compare. For an absolute number to quote, run the base with `quality` confirm (finer mesh), or let phase 5 confirm the winner. Findings and the summary quote confirm numbers; a screen number is allowed in `conclude_suspect` when you say it is a screen result.
6. A run that failed its checks cannot support anything: say what failed, change the setup, or stop that idea.
7. `conclude_suspect` for each suspect (ruled out only after two test points), `record_finding` for an improvement (cite the run's evidence code and its assumptions row), then `close_investigation`.

Defaults: a number from a run that used a default input is about the default, not the board. Write it as conditional ("with the assumed 1 A load, the simulation gives ..."). The server lists the default inputs in the finding, refuses a default-based number stated as fact, and does not allow high or critical on one. Get the real input and run again to lift that.

Signal: where a signal run covers a net, its field-solved impedance is the impedance (cite it). Give the analyzer's closed-form figure only as the estimate it replaces (it ignores coplanar ground beside the track), never both as if they agreed, and take any width for a target from the solve. A net with `beyond_quasi_static` is above the model's limit: say the number is a quasi-static estimate, not valid at its band. Heat: for thermal, `heat_from_power` adds the copper loss of the latest power run (as certain as that run's currents).
