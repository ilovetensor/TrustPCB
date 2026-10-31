This is a recheck of version {version_number}. Start from the previous review (its id is {previous_review_id}), not from scratch. The previous version had these open findings:

{findings}

How a recheck works:
- Every code phase has run again on the new version. Its analyzer and KiCad results are in `list_candidates`, and the server matches your findings with the previous ones by fingerprint.
- For each previous finding, find out whether the issue is fixed, still present or changed. Record a finding again where the issue remains (the same fingerprint carries its history and any dismissal made on the earlier version). Say nothing about a previous finding that is gone from the new results: the server reports it as fixed.
- Look for new issues too: anything in the new results that no previous finding covers is new.
- Investigate only what changed. {changes}
- At the end, the summary says how many findings are fixed, still present and new, in plain words. The server lists them in the report under "Changes since the previous version".
