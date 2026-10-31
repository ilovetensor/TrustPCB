This is a recheck. You are reviewing version {version_number}. The previous version is version {previous_version_number}, and its review has the id {previous_review_id}. Start from that review, not from scratch. Version {previous_version_number} had these open findings:

{findings}

{previous_result}

How a recheck works:
- Every code phase has run again on version {version_number}. Its analyzer and KiCad results are in `list_candidates`, and the server matches your findings with the previous ones by fingerprint and by the results they cover.
- For each previous finding, find out whether the issue is fixed, still present or changed. Record a finding again where the issue remains (the same fingerprint carries its history and any dismissal made on the earlier version). Say nothing about a previous finding that is gone from the new results: the server reports it as fixed.
- Look for new issues too: anything in the new results that no previous finding covers is new. A result the previous review already had, which it only cleared as harmless, is not new.
- Investigate only what changed. {changes}

What the summary and the antenna finding must say:
- Start with what changed since version {previous_version_number}: the as-built worst S11 across the band in version {previous_version_number} and in version {version_number}, the accepted power at the worst point and its mean, and whether version {version_number} is better or worse. Say "compared with version {previous_version_number}". Name the version numbers exactly: {previous_version_number} is the old one, {version_number} is the one you are checking.
- Say whether the earlier prediction held: whether the result measured on version {version_number} matches what the review of version {previous_version_number} predicted for its fix, within 1 dB. If the parts were not changed as recommended, say so instead.
- Say plainly whether the tuning target is met. Missing the target does not cancel an improvement: report the gain first, then the gap that is left.
- Only after that, give the counts for the findings: fixed, improved, still present, worse and new. The server lists them in the report under "Changes since the previous version". A finding whose results are the same as before, only grouped differently, is still present, not new.
- At phase 6 the server gives you the exact opening sentences (`lead_the_summary_with`). A summary that does not open with them is refused.
