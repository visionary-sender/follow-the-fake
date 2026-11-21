"""Template fingerprint: MinHash over word 5-shingles of the name-masked text.

Exported as a hex ``text_template`` value (128 x uint32, big-endian). The fraction of equal
positions in two signatures estimates the Jaccard similarity of the masked shingle sets
(standard error about 0.044 at 128 permutations). Thresholds belong to the linking engine.
"""

from __future__ import annotations

import hashlib

import numpy as np

SHINGLE = 5
NUM_PERM = 128
_PRIME = np.uint64((1 << 61) - 1)
_rng = np.random.default_rng(20261121)  # fixed seed: signatures must be comparable across runs
_A = _rng.integers(1, 1 << 32, NUM_PERM, dtype=np.uint64)
_B = _rng.integers(0, 1 << 32, NUM_PERM, dtype=np.uint64)


def shingles(tokens: list[str], k: int = SHINGLE) -> set[str]:
    """Word k-shingles; empty when the text has fewer than k tokens (too short to compare)."""
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def minhash(sh: set[str]) -> np.ndarray:
    """MinHash signature (NUM_PERM uint32) of a non-empty shingle set."""
    if not sh:
        raise ValueError("no shingles: text too short for a template signature")
    hv = np.array(
        [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=4).digest(), "big") for s in sh],
        dtype=np.uint64,
    )
    # a, hv < 2**32, so a*hv + b < 2**64: no overflow before the Mersenne modulus.
    ph = (np.outer(hv, _A) + _B) % _PRIME
    return (ph.min(axis=0) & np.uint64(0xFFFFFFFF)).astype(np.uint32)


def to_hex(sig: np.ndarray) -> str:
    return sig.astype(">u4").tobytes().hex()


def from_hex(value: str) -> np.ndarray:
    return np.frombuffer(bytes.fromhex(value), dtype=">u4").astype(np.uint32)


def estimate(a: str, b: str) -> float:
    """Estimated masked-shingle Jaccard from two hex signatures."""
    return float(np.mean(from_hex(a) == from_hex(b)))
