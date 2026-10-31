## Extent and inputs

This review's extent: {extent_text}. Do only these checks; anything else is not part of it.

Each board simulation needs inputs (a load current, a part's power, an ambient temperature). Settle them now, with `get_sim_inputs` for each kind you will run:
- Take a value from the engineer's request when it states one, with `set_sim_input` source `user`. The value must appear in the request; the server checks.
- {datasheet_line}
- When datasheets are on, look up the main parts before leaving this phase: the radio and the regulator at least (`lookup_part` with the exact value on the symbol). If the request states something measurable, such as a transmit power in dBm, read the radio's datasheet (`read_datasheet` with `mpn` and a question) for the supply current at that output; do not guess it. The server refuses `advance_phase` while these are missing and says exactly what to call.
- Anything nobody gave stays a labelled default. Never invent a number, and never pick a convenient one. A result that rests on a default is conditional, and every finding that quotes it must say so.

Do not ask the engineer questions: record what you assumed in the brief.
