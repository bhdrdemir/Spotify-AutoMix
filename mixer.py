#!/usr/bin/env python3
"""spotify-automix: build harmonically mixed Spotify playlists.

Orders tracks by BPM + Camelot key compatibility (like Spotify's own
crossfade/mix view shows) so transitions flow smoothly, smoothest first.

Usage:
  python mixer.py <playlist-url>                      # mix the whole playlist
  python mixer.py <playlist-url> --select             # pick tracks interactively
  python mixer.py <playlist-url> --seed "song name"   # 10-track mix from a seed song
  python mixer.py liked --seed "daft punk" --count 15 # use your Liked Songs as pool
  python mixer.py <playlist-url> --dry-run            # print order, don't create
"""

import argparse
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from features import enrich_tracks
from sequencer import order_tracks, seed_mix
from spotify_client import get_client, get_tracks, create_playlist


def parse_selection(expr: str, n: int) -> list[int]:
    """'1,3,5-8' -> [0, 2, 4, 5, 6, 7] (0-based, bounded by n)."""
    chosen = []
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            chosen.extend(range(start - 1, end))
        else:
            try:
                chosen.append(int(part) - 1)
            except ValueError:
                continue
    return sorted({i for i in chosen if 0 <= i < n})


def select_interactive(tracks: list[dict]) -> list[dict]:
    print("\nTracks in the pool:")
    for i, t in enumerate(tracks, 1):
        bpm = f"{t['bpm']:.0f}" if t.get("bpm") else "?"
        cam = t.get("camelot") or "?"
        print(f"  {i:3d}. {t['artist']} - {t['name']}  [{bpm} BPM, {cam}]")
    expr = input("\nSelect tracks (e.g. 1,3,5-12) or press Enter for all: ").strip()
    if not expr:
        return tracks
    idx = parse_selection(expr, len(tracks))
    if len(idx) < 2:
        sys.exit("Need at least 2 tracks.")
    return [tracks[i] for i in idx]


def find_seed(tracks: list[dict], query: str) -> dict:
    q = query.lower()
    matches = [t for t in tracks if q in f"{t['artist']} - {t['name']}".lower()]
    if not matches:
        sys.exit(f"No track matching '{query}' found in the pool.")
    if len(matches) == 1:
        return matches[0]
    print(f"\nMultiple matches for '{query}':")
    for i, t in enumerate(matches, 1):
        print(f"  {i}. {t['artist']} - {t['name']}")
    while True:
        try:
            pick = int(input("Pick one: "))
            if 1 <= pick <= len(matches):
                return matches[pick - 1]
        except ValueError:
            pass


def print_chain(chain: list[tuple[dict, float | None]]) -> None:
    print(f"\n{'#':>3}  {'Track':<55} {'BPM':>6}  {'Key':>4}  {'Mix':>5}")
    print("-" * 80)
    for i, (t, score) in enumerate(chain, 1):
        name = f"{t['artist']} - {t['name']}"
        if len(name) > 54:
            name = name[:51] + "..."
        bpm = f"{t['bpm']:.0f}" if t.get("bpm") else "?"
        cam = t.get("camelot") or "?"
        s = f"{score:.0f}" if score is not None else "--"
        print(f"{i:>3}  {name:<55} {bpm:>6}  {cam:>4}  {s:>5}")
    scores = [s for _, s in chain if s is not None]
    if scores:
        print("-" * 80)
        print(f"Average transition score: {sum(scores) / len(scores):.1f}/100")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create harmonically mixed Spotify playlists (BPM + Camelot key)."
    )
    ap.add_argument("source", help="Playlist URL/ID, or 'liked' for your saved tracks")
    ap.add_argument("--select", action="store_true", help="Pick tracks interactively")
    ap.add_argument("--seed", metavar="QUERY", help="Build a mix starting from this song")
    ap.add_argument("--count", type=int, default=10, help="Mix length in seed mode (default 10)")
    ap.add_argument("--name", help="Name for the created playlist")
    ap.add_argument("--dry-run", action="store_true", help="Print the order, don't create a playlist")
    ap.add_argument(
        "--keep-unknown", action="store_true",
        help="Keep tracks without BPM/key data in the pool (scored neutrally)",
    )
    ap.add_argument(
        "--analyze-all", action="store_true",
        help="Slow: audio-analyze every track missing from the database "
             "(seconds per track). By default only the seed is analyzed.",
    )
    args = ap.parse_args()
    args.count = max(2, min(args.count, 20))

    sp = get_client()
    print("Fetching tracks from Spotify...")
    tracks = get_tracks(sp, args.source)
    if len(tracks) < 2:
        sys.exit("Pool has fewer than 2 usable tracks.")
    print(f"Found {len(tracks)} track(s).")

    tracks = enrich_tracks(tracks, deep=args.analyze_all)

    if not args.keep_unknown:
        known = [t for t in tracks if t.get("bpm") or t.get("camelot")]
        dropped = len(tracks) - len(known)
        if dropped:
            print(f"Excluding {dropped} track(s) without BPM/key data "
                  f"(use --keep-unknown to include them).")
        if args.seed:
            # keep the seed even if it has no data
            seed_kept = find_seed(tracks, args.seed)
            if seed_kept not in known:
                known.append(seed_kept)
        tracks = known
        if len(tracks) < 2:
            sys.exit("Not enough tracks with BPM/key data.")

    if args.seed:
        seed = find_seed(tracks, args.seed)
        if not seed.get("bpm") and not seed.get("camelot"):
            print("Seed not in database - trying alternate releases...")
            from features import deep_enrich_one, enrich_from_alias
            from spotify_client import find_alias_ids
            if not enrich_from_alias(seed, find_alias_ids(sp, seed)):
                print("No alternate release found - analyzing preview audio...")
                deep_enrich_one(seed)
        if not seed.get("bpm") and not seed.get("camelot"):
            print(
                "\n! Couldn't get data for the seed (no preview audio). "
                "Add it to overrides.csv for a proper mix."
            )
        print(f"\nSeed: {seed['artist']} - {seed['name']} "
              f"[{seed.get('bpm') or '?'} BPM, {seed.get('camelot') or '?'}]")
        chain = seed_mix(tracks, seed, count=args.count)
    else:
        if args.select:
            tracks = select_interactive(tracks)
        chain = order_tracks(tracks)

    print_chain(chain)

    if args.dry_run:
        return

    answer = input("\nCreate this playlist on Spotify? [y/N] ").strip().lower()
    if answer != "y":
        print("Cancelled.")
        return

    if args.seed:
        default_name = f"Auto Mix - {chain[0][0]['name']}"
    else:
        default_name = "Auto Mix"
    name = args.name or default_name
    url = create_playlist(
        sp, name, [t["uri"] for t, _ in chain],
        description="Harmonic auto-mix (BPM + Camelot) by spotify-automix",
    )
    print(f"\nDone! Playlist created: {url}")


if __name__ == "__main__":
    main()
