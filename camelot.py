"""Camelot wheel utilities: pitch-class/mode -> Camelot code + harmonic compatibility scoring."""

# Pitch class (0-11) -> Camelot code
# Mode: 1 = major (B side), 0 = minor (A side)
CAMELOT_MAJOR = {
    0: "8B",   # C
    1: "3B",   # C#/Db
    2: "10B",  # D
    3: "5B",   # D#/Eb
    4: "12B",  # E
    5: "7B",   # F
    6: "2B",   # F#/Gb
    7: "9B",   # G
    8: "4B",   # G#/Ab
    9: "11B",  # A
    10: "6B",  # A#/Bb
    11: "1B",  # B
}

CAMELOT_MINOR = {
    0: "5A",   # Cm
    1: "12A",  # C#m
    2: "7A",   # Dm
    3: "2A",   # D#m/Ebm
    4: "9A",   # Em
    5: "4A",   # Fm
    6: "11A",  # F#m
    7: "6A",   # Gm
    8: "1A",   # G#m/Abm
    9: "8A",   # Am
    10: "3A",  # A#m/Bbm
    11: "10A", # Bm
}


def to_camelot(key: int, mode: int) -> str | None:
    """Convert pitch class + mode to a Camelot code like '8A'. Returns None if unknown."""
    if key is None or key < 0 or key > 11 or mode not in (0, 1):
        return None
    return CAMELOT_MAJOR[key] if mode == 1 else CAMELOT_MINOR[key]


def parse_camelot(code: str) -> tuple[int, str] | None:
    """'8A' -> (8, 'A'). Returns None if invalid."""
    if not code:
        return None
    code = code.strip().upper()
    if len(code) < 2 or code[-1] not in ("A", "B"):
        return None
    try:
        num = int(code[:-1])
    except ValueError:
        return None
    if not 1 <= num <= 12:
        return None
    return num, code[-1]


def key_score(c1: str | None, c2: str | None) -> float:
    """Harmonic compatibility between two Camelot codes (0-100).

    100  same key            (8A -> 8A)
     90  +/-1 on the wheel   (8A -> 9A / 7A)
     85  relative key        (8A -> 8B)
     70  +2 energy boost     (8A -> 10A)
     55  diagonal mix        (8A -> 9B)
     20  everything else
     50  neutral, if a key is unknown
    """
    p1, p2 = parse_camelot(c1) if c1 else None, parse_camelot(c2) if c2 else None
    if p1 is None or p2 is None:
        return 50.0
    n1, l1 = p1
    n2, l2 = p2
    diff = (n2 - n1) % 12  # 0..11 on the wheel

    if l1 == l2:
        if diff == 0:
            return 100.0
        if diff in (1, 11):
            return 90.0
        if diff == 2:
            return 70.0
        return 20.0
    # different letter (A <-> B)
    if diff == 0:
        return 85.0
    if diff in (1, 11):
        return 55.0
    return 20.0
