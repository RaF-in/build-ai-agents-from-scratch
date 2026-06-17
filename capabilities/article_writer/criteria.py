"""Gradable criteria for article writing — the SAME weighted/thresholded Criterion
shape the deep-research pack uses, just different dimensions. Injected into BOTH the
generator (steers before feedback) and the evaluator (Phase 4 scoring).

This file existing — and nothing in the shared engine changing to read it — is half
the Phase 8 proof: a capability differs only in its data.
"""
from capabilities.shared.config import Criterion

WRITING_CRITERIA = [
    Criterion("structure",   0.25, 0.6, "Clear arc: hook -> argument -> payoff?"),
    Criterion("originality", 0.30, 0.6, "Distinct voice, not generic AI filler?"),
    Criterion("accuracy",    0.25, 0.7, "Claims supported; no fabrication?"),
    Criterion("craft",       0.20, 0.5, "Prose, rhythm, transitions?"),
]
