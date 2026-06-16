"""Gradable criteria for deep research (the article's "make quality gradable" idea
as data). The SAME list is injected into BOTH the generator and evaluator prompts:
the criteria wording steers the generator before any evaluator feedback exists.

Weighted scoring + thresholds are consumed for real by the evaluator in Phase 4;
defined here so RESEARCH_CONFIG is already complete.
"""
from capabilities.shared.config import Criterion

RESEARCH_CRITERIA = [
    Criterion("coverage",         0.30, 0.6, "Are all sub-questions answered with evidence?"),
    Criterion("citation_quality", 0.30, 0.7, "Does every factual claim cite a resolvable source?"),
    Criterion("source_quality",   0.20, 0.6, "Primary/authoritative sources over SEO filler?"),
    Criterion("synthesis",        0.20, 0.5, "A coherent answer, not a link dump?"),
]
