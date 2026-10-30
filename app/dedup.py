"""Clip comparison + duplicate verdicts.

The job: before you spend time searching a clip (or worse, use it in another
video) tell you whether it is footage you already have.

We compare *bags of frames* rather than sequences, because a clip reused in a
different edit will be trimmed/reordered.  Every frame of the smaller clip is
allowed to find its own best partner in the other clip; how many find a good
partner is the "coverage", how close those partners are is the "distance".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import DUP_COVERAGE, DUP_MEAN_DISTANCE, NEAR_COVERAGE, NEAR_MEAN_DISTANCE
from .hashing import Fingerprint, frame_matrix

MATCH_DISTANCE = 0.32  # a single frame pair counts as "matched" below this


@dataclass
class Comparison:
    score: float                 # 0..1, higher = same footage
    verdict: str                 # "duplicate" | "near" | "distinct"
    coverage_a: float            # share of A's frames found in B
    coverage_b: float            # share of B's frames found in A
    mean_distance: float         # mean distance of matched pairs
    matched_frames: int
    frame_count: int
    reasons: list[str] = field(default_factory=list)

    @property
    def percent(self) -> int:
        return round(self.score * 100)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "verdict": self.verdict,
            "coverage_a": round(self.coverage_a, 3),
            "coverage_b": round(self.coverage_b, 3),
            "mean_distance": round(self.mean_distance, 4),
            "matched_frames": self.matched_frames,
            "frame_count": self.frame_count,
            "reasons": self.reasons,
            "percent": self.percent,
        }


def compare(
    a: Fingerprint,
    b: Fingerprint,
    dup_mean: float = DUP_MEAN_DISTANCE,
    dup_cov: float = DUP_COVERAGE,
) -> Comparison:
    if not a.frames or not b.frames:
        return Comparison(0.0, "distinct", 0.0, 0.0, 1.0, 0, 0, ["one of the clips has no frames"])

    matrix = frame_matrix(a, b)
    best_a = [min(row) for row in matrix]                      # for each frame of a
    best_b = [min(matrix[r][c] for r in range(len(a.frames)))  # for each frame of b
              for c in range(len(b.frames))]

    # A short clip inside a long one should not be punished for the long clip's
    # extra frames, so coverage is measured on the *smaller* side.
    if len(a.frames) <= len(b.frames):
        primary, other = best_a, best_b
        cov_a = sum(1 for d in best_a if d <= MATCH_DISTANCE) / len(best_a)
        cov_b = sum(1 for d in best_b if d <= MATCH_DISTANCE) / len(best_b)
    else:
        primary, other = best_b, best_a
        cov_a = sum(1 for d in best_a if d <= MATCH_DISTANCE) / len(best_a)
        cov_b = sum(1 for d in best_b if d <= MATCH_DISTANCE) / len(best_b)

    matched = [d for d in primary if d <= MATCH_DISTANCE]
    coverage = len(matched) / len(primary) if primary else 0.0
    mean_distance = sum(matched) / len(matched) if matched else 1.0

    # Score blends "how much of it lines up" with "how close the matches are".
    closeness = max(0.0, 1.0 - mean_distance / MATCH_DISTANCE)
    score = (0.65 * coverage + 0.35 * closeness) if matched else 0.0

    verdict = "distinct"
    reasons: list[str] = []
    if coverage >= dup_cov and mean_distance <= dup_mean:
        verdict = "duplicate"
        reasons.append(
            f"{round(coverage * 100)}% of the sampled frames line up (avg distance {mean_distance:.2f})"
        )
        if len(a.frames) != len(b.frames):
            big, small = (a, b) if len(a.frames) > len(b.frames) else (b, a)
            if small.duration and big.duration and small.duration < big.duration * 0.75:
                reasons.append("one clip looks like an excerpt of the other")
            elif abs(len(a.frames) - len(b.frames)) > 1:
                reasons.append("clips have different lengths but matching footage")
        if a.width and b.width and a.width != b.width:
            reasons.append("different resolution — so this is a re-upload/re-edit, not the same file")
    elif coverage >= NEAR_COVERAGE or mean_distance <= NEAR_MEAN_DISTANCE:
        verdict = "near"
        reasons.append("some frames look alike — worth a quick eyeball")
    else:
        reasons.append("no overlapping footage found")

    return Comparison(
        score=score,
        verdict=verdict,
        coverage_a=cov_a,
        coverage_b=cov_b,
        mean_distance=mean_distance,
        matched_frames=len(matched),
        frame_count=len(primary),
        reasons=reasons,
    )
