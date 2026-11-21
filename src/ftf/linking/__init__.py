"""Linking engine: evidence ledgers between captures, groupings per relation, open-set assignment."""

from ftf.linking.assign import IncidentResult, assign, link_incident
from ftf.linking.grouping import ALL_RELATIONS, cluster, group, link_dataset
from ftf.linking.types import METHOD, PairScore, PairScorer

__all__ = [
    "ALL_RELATIONS",
    "METHOD",
    "IncidentResult",
    "PairScore",
    "PairScorer",
    "assign",
    "cluster",
    "group",
    "link_dataset",
    "link_incident",
]
