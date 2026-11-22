"""End-to-end analysis of one submitted item (the integrator).

`analyze(AnalysisInput, base)` runs ingest → content features → enrichment → link/assign and
returns an `AnalysisResult` (contract `Incident`, `Link`s, `Assignment`, similar base items and
one `StageReport` per stage). Each stage is a small adapter in `ftf.pipeline.stages` that calls
the owning project's library when it is importable and reports `not_available` otherwise.
"""

from ftf.pipeline.analyze import (
    INPUT_KINDS,
    AnalysisInput,
    AnalysisResult,
    SimilarItem,
    StageReport,
    analyze,
    guess_kind,
)

__all__ = [
    "INPUT_KINDS",
    "AnalysisInput",
    "AnalysisResult",
    "SimilarItem",
    "StageReport",
    "analyze",
    "guess_kind",
]
