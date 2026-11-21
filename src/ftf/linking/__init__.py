"""Linking engine: evidence ledgers between captures (ledger.py), groupings per relation (grouping.py),
open-set assignment (assign.py), candidate index (index.py), fragile-link flags (sensitivity.py)."""

from ftf.linking.assign import IncidentResult, assign, link_incident
from ftf.linking.grouping import ALL_RELATIONS, cluster, group, link_dataset
from ftf.linking.index import CandidateIndex
from ftf.linking.ledger import ScoringContext, score_pair, threshold, to_link
from ftf.linking.sensitivity import fragile_links
from ftf.linking.types import METHOD, PairScore, PairScorer
from ftf.linking.weights import HAND_TABLE

__all__ = [
    "ALL_RELATIONS",
    "CandidateIndex",
    "HAND_TABLE",
    "METHOD",
    "IncidentResult",
    "PairScore",
    "PairScorer",
    "ScoringContext",
    "assign",
    "cluster",
    "fragile_links",
    "group",
    "link_dataset",
    "link_incident",
    "score_pair",
    "threshold",
    "to_link",
]
