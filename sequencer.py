"""Transition scoring and track ordering (greedy chain)."""

from camelot import key_score

KEY_WEIGHT = 0.45
BPM_WEIGHT = 0.35
VIBE_WEIGHT = 0.20

# BPM difference tolerance (as a fraction of the source BPM)
BPM_PERFECT = 0.02   # <=2% -> 100
BPM_LIMIT = 0.06     # >=6% -> 0
HALF_DOUBLE_CAP = 85.0  # max score when matching at half/double tempo


def bpm_score(b1: float | None, b2: float | None) -> float:
    """BPM compatibility (0-100). Considers half/double tempo matches."""
    if not b1 or not b2:
        return 50.0  # neutral when unknown
    best = 0.0
    for mult, cap in ((1.0, 100.0), (0.5, HALF_DOUBLE_CAP), (2.0, HALF_DOUBLE_CAP)):
        diff = abs(b2 * mult - b1) / b1
        if diff <= BPM_PERFECT:
            score = 100.0
        elif diff >= BPM_LIMIT:
            score = 0.0
        else:
            score = 100.0 * (BPM_LIMIT - diff) / (BPM_LIMIT - BPM_PERFECT)
        best = max(best, min(score, cap))
    return best


VIBE_DIMS = ("energy", "danceability", "valence", "acousticness")
VIBE_FULL_MISMATCH = 0.6  # mean difference at which the score reaches 0


def vibe_score(t1: dict, t2: dict) -> float:
    """Sound-character similarity (0-100) from audio features.

    Compares energy, danceability, valence and acousticness (each 0-1).
    Tracks from unrelated styles (e.g. orchestral vs phonk) differ strongly
    on these axes, so this acts as a genre-compatibility signal without
    needing any genre metadata. Neutral 50 when data is missing.
    """
    diffs = []
    for d in VIBE_DIMS:
        a, b = t1.get(d), t2.get(d)
        if a is not None and b is not None:
            diffs.append(abs(a - b))
    if not diffs:
        return 50.0
    mean_diff = sum(diffs) / len(diffs)
    return max(0.0, 100.0 * (1.0 - mean_diff / VIBE_FULL_MISMATCH))


def transition_score(t1: dict, t2: dict) -> float:
    """Overall transition quality from track t1 to t2 (0-100).

    Tracks are dicts with 'bpm', 'camelot' and audio feature keys.
    """
    ks = key_score(t1.get("camelot"), t2.get("camelot"))
    bs = bpm_score(t1.get("bpm"), t2.get("bpm"))
    vs = vibe_score(t1, t2)
    return KEY_WEIGHT * ks + BPM_WEIGHT * bs + VIBE_WEIGHT * vs


def _best_next(current: dict, candidates: list[dict]) -> tuple[int, float]:
    """Index + score of the most compatible next track among candidates."""
    best_i, best_s = 0, -1.0
    for i, cand in enumerate(candidates):
        s = transition_score(current, cand)
        if s > best_s:
            best_i, best_s = i, s
    return best_i, best_s


def order_tracks(tracks: list[dict]) -> list[tuple[dict, float | None]]:
    """Order all tracks into a mix chain.

    Starts with the single most compatible pair, then greedily appends the
    best next track. Result tends toward descending transition quality.
    Returns [(track, score_from_previous)], first item has score None.
    """
    if len(tracks) < 2:
        return [(t, None) for t in tracks]

    # find the best starting pair
    best = (0, 1, -1.0)
    for i in range(len(tracks)):
        for j in range(len(tracks)):
            if i == j:
                continue
            s = transition_score(tracks[i], tracks[j])
            if s > best[2]:
                best = (i, j, s)
    i, j, s = best

    remaining = [t for k, t in enumerate(tracks) if k not in (i, j)]
    chain = [(tracks[i], None), (tracks[j], s)]
    current = tracks[j]
    while remaining:
        bi, bs = _best_next(current, remaining)
        current = remaining.pop(bi)
        chain.append((current, bs))
    return chain


def seed_mix(tracks: list[dict], seed: dict, count: int = 10) -> list[tuple[dict, float | None]]:
    """Build a mix of `count` tracks starting from `seed`, picking the most
    compatible next track at each step from the remaining pool."""
    remaining = [t for t in tracks if t["id"] != seed["id"]]
    chain = [(seed, None)]
    current = seed
    while remaining and len(chain) < count:
        bi, bs = _best_next(current, remaining)
        current = remaining.pop(bi)
        chain.append((current, bs))
    return chain
