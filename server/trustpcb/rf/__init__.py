"""RF model: from a parsed board to a solver job, and from solver output to a trusted result.

Modules: setup (what to simulate), geometry (copper merge and triangles), mesh, scenario (cache key
and job building), whatif (typed model changes), queue (Docker runs), postprocess (trust and
summary), matching (as-built and E24 tuning), lab (the calls the review uses).
"""
