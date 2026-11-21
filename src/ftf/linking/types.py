"""Shared types of the linking engine (frozen interface; change only via the Linking engine coordinator).

Evaluation and the demo app build against these names. Unit of scoring = capture (contract `Capture.id`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ftf.contract.models import EvidenceRow, Relation

METHOD = "ledger@0.1"


@dataclass(frozen=True)
class PairScore:
    """Evidence ledger for one pair of captures under one relation."""

    a: str  # capture id
    b: str  # capture id
    relation: Relation
    bits: float  # sum over layers, before the prior / threshold
    rows: list[EvidenceRow] = field(default_factory=list)  # itemised ledger, one row per indicator kind
    layers_positive: int = 0  # layers with a net positive contribution
    live_private: bool = False  # a live operator-private id is shared (tenant/owner/tracker instance/live pixel)


class PairScorer(Protocol):
    """Anything that scores two captures under a relation (the hand-table ledger, later the calibrated one)."""

    def __call__(self, a: str, b: str, relation: Relation) -> PairScore: ...
