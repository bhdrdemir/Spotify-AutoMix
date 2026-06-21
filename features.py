"""Fetch BPM + key data: ReccoBeats API -> local cache -> manual overrides.csv."""

import csv
import json
import os
import re
import time

import requests

from camelot import to_camelot, parse_camelot

RECCOBEATS_URL = "https://api.reccobeats.com/v1/audio-features"
RECCOBEATS_ANALYSIS_URL = "https://api.reccobeats.com/v1/analysis/audio-features"
RECCOBEATS_RECO_URL = "https://api.reccobeats.com/v1/track/recommendation"
RECCOBEATS_TRACK_URL = "https://api.reccobeats.com/v1/track"
SPOTIFY_EMBED_URL = "https://open.spotify.com/embed/track/{}"
SOUNDSTAT_URL = "https://soundstat.info/api/v1/track"
BATCH_SIZE = 40
CACHE_FILE = "cache.json"
OVERRIDES_FILE = "overrides.csv"

_SPOTIFY_ID_RE = re.compile(r"track[/:]([A-Za-z0-9]{22})")


def _load_cache(path: str = CACHE_FILE) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict, path: str = CACHE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _load_overrides(path: str = OVERRIDES_FILE) -> dict:
    """overrides.csv columns: match,bpm,camelot
    `match` is either a Spotify track ID or a case-insensitive
    'artist - title' string."""
    overrides = {}
    if not os.path.exists(path):
        return overrides
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            match = (row.get("match") or "").strip()
            if not match:
                continue
            try:
                bpm = float(row.get("bpm") or 0) or None
            except ValueError:
                bpm = None
            camelot = (row.get("camelot") or "").strip().upper() or None
            if camelot and not parse_camelot(camelot):
                print(f"  ! overrides.csv: invalid Camelot code '{camelot}' for '{match}', ignored")
                camelot = None
            overrides[match.lower()] = {"bpm": bpm, "camelot": camelot}
    return overrides


def _extract_spotify_id(item: dict) -> str | None:
    href = item.get("href") or ""
    m = _SPOTIFY_ID_RE.search(href)
    return m.group(1) if m else None


def _fetch_batch(ids: list[str]) -> dict:
    """Fetch a batch of Spotify IDs from ReccoBeats. Returns {spotify_id: features}."""
    resp = requests.get(RECCOBEATS_URL, params={"ids": ",".join(ids)}, timeout=30)
    resp.raise_for_status()
    content = resp.json().get("content", [])
    out = {}
    for idx, item in enumerate(content):
        sid = _extract_spotify_id(item)
        if sid is None and len(content) == len(ids):
            sid = ids[idx]  # positional fallback when counts match
        if sid is None:
            continue
        out[sid] = {
            "bpm": round(float(item["tempo"]), 1) if item.get("tempo") else None,
            "camelot": to_camelot(item.get("key", -1), item.get("mode", -1)),
            "energy": item.get("energy"),
            "danceability": item.get("danceability"),
            "valence": item.get("valence"),
            "acousticness": item.get("acousticness"),
        }
    return out


_PREVIEW_RE = re.compile(r'"audioPreview"\s*:\s*\{\s*"url"\s*:\s*"([^"]+)"')


def _find_preview_url(track: dict) -> str | None:
    """Spotify 30s preview MP3 url - from the track object, or the public
    embed page (preview_url is no longer returned to new API apps)."""
    if track.get("preview_url"):
        return track["preview_url"]
    try:
        resp = requests.get(
            SPOTIFY_EMBED_URL.format(track["id"]),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        m = _PREVIEW_RE.search(resp.text)
        return m.group(1).replace("\\u002F", "/").replace("\\/", "/") if m else None
    except requests.RequestException:
        return None


def _analyze_preview(preview_url: str) -> dict | None:
    """Download the preview MP3 and analyze it with ReccoBeats (tempo + vibe)."""
    audio = requests.get(preview_url, timeout=30)
    audio.raise_for_status()
    if len(audio.content) > 5 * 1024 * 1024:
        return None
    resp = requests.post(
        RECCOBEATS_ANALYSIS_URL,
        files={"audioFile": ("preview.mp3", audio.content, "audio/mpeg")},
        timeout=60,
    )
    resp.raise_for_status()
    feats = resp.json()
    tempo = feats.get("tempo")
    if not tempo:
        return None
    return {
        "bpm": round(float(tempo), 1),
        "energy": feats.get("energy"),
        "danceability": feats.get("danceability"),
        "valence": feats.get("valence"),
        "acousticness": feats.get("acousticness"),
    }


def preview_fill(tracks: list[dict], cache: dict, cache_path: str) -> None:
    """Fill missing BPMs by analyzing Spotify preview audio via ReccoBeats.

    Free, no key needed. Note: the analysis endpoint returns tempo but not
    key, so Camelot stays unknown for these tracks (scored neutrally)."""
    todo = [
        t for t in tracks
        if cache.get(t["id"], {}).get("bpm") is None
        and not cache.get(t["id"], {}).get("preview_tried")
    ]
    if not todo:
        return
    print(
        f"Preview analysis: getting BPM for {len(todo)} track(s) from 30s "
        f"previews (one-time, ~1-2s each)..."
    )
    done = found = 0
    try:
        for t in todo:
            done += 1
            print(f"\r  {done}/{len(todo)} ({found} found)", end="", flush=True)
            entry = cache.setdefault(
                t["id"], {"bpm": None, "camelot": None, "energy": None}
            )
            entry["preview_tried"] = True
            url = _find_preview_url(t)
            if not url:
                continue
            try:
                feats = _analyze_preview(url)
            except requests.RequestException:
                continue
            if feats:
                entry.update(feats)
                found += 1
            if done % 10 == 0:
                _save_cache(cache, cache_path)
            time.sleep(0.4)  # be polite
        print()
    except KeyboardInterrupt:
        print("\n  Interrupted - progress saved, rerun to continue.")
    finally:
        _save_cache(cache, cache_path)


def _fetch_soundstat(track_id: str, api_key: str) -> dict | None:
    """Fetch one track from SoundStat (analyzes on demand; 202 = still analyzing)."""
    headers = {"x-api-key": api_key}
    for attempt in range(3):
        resp = requests.get(f"{SOUNDSTAT_URL}/{track_id}", headers=headers, timeout=30)
        if resp.status_code == 202:
            time.sleep(4)  # analysis in progress, wait and retry
            continue
        if resp.status_code in (402, 403, 429):
            raise PermissionError(f"SoundStat limit/auth issue (HTTP {resp.status_code})")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        feats = resp.json().get("features", {})
        if not feats:
            return None
        return {
            "bpm": round(float(feats["tempo"]), 1) if feats.get("tempo") else None,
            "camelot": to_camelot(feats.get("key", -1), feats.get("mode", -1)),
            "energy": feats.get("energy"),
            "danceability": feats.get("danceability"),
            "valence": feats.get("valence"),
            "acousticness": feats.get("acousticness"),
        }
    return None  # still analyzing after retries - will pick it up next run


def soundstat_fill(tracks: list[dict], cache: dict, cache_path: str) -> None:
    """Fill tracks missing from ReccoBeats using SoundStat (needs SOUNDSTAT_KEY env)."""
    api_key = os.getenv("SOUNDSTAT_KEY")
    if not api_key:
        return
    still_missing = [
        t["id"] for t in tracks
        if (cache.get(t["id"], {}).get("bpm") is None
            or cache.get(t["id"], {}).get("camelot") is None)
        and not cache.get(t["id"], {}).get("soundstat_tried")
    ]
    if not still_missing:
        return
    print(f"SoundStat: analyzing {len(still_missing)} remaining track(s)...")
    done = 0
    try:
        for tid in still_missing:
            done += 1
            print(f"\r  {done}/{len(still_missing)}", end="", flush=True)
            try:
                feats = _fetch_soundstat(tid, api_key)
            except PermissionError as e:
                print(f"\n  ! {e} - stopping SoundStat lookups for now.")
                break
            except requests.RequestException as e:
                print(f"\n  ! SoundStat error: {e}")
                continue
            if feats:
                feats["soundstat_tried"] = True
                cache[tid] = feats
            else:
                cache.setdefault(
                    tid, {"bpm": None, "camelot": None, "energy": None}
                )["soundstat_tried"] = True
            if done % 10 == 0:
                _save_cache(cache, cache_path)
        print()
    except KeyboardInterrupt:
        print("\n  Interrupted - progress saved.")
    finally:
        _save_cache(cache, cache_path)


def enrich_tracks(tracks: list[dict], cache_dir: str = ".", deep: bool = False) -> list[dict]:
    """Attach 'bpm' and 'camelot' to each track dict (must have 'id', 'name', 'artist').

    Priority: overrides.csv > cache.json > ReccoBeats API.
    Tracks with no data at all are kept but flagged (bpm/camelot = None).

    deep=False (default): only the fast ReccoBeats database lookup.
    deep=True: additionally analyze missing tracks one by one from preview
    audio (slow - seconds per track; use for small pools or single tracks).
    """
    cache_path = os.path.join(cache_dir, CACHE_FILE)
    overrides_path = os.path.join(cache_dir, OVERRIDES_FILE)
    cache = _load_cache(cache_path)
    overrides = _load_overrides(overrides_path)

    # fetch missing from API (also re-fetch old cache entries that predate
    # the vibe fields - danceability/valence/acousticness)
    missing = [
        t["id"] for t in tracks
        if t["id"] not in cache
        or ("danceability" not in cache[t["id"]]
            and cache[t["id"]].get("bpm") is not None
            and not cache[t["id"]].get("preview_tried"))
    ]
    if missing:
        total_batches = (len(missing) + BATCH_SIZE - 1) // BATCH_SIZE
        print(
            f"ReccoBeats: fetching audio features for {len(missing)} track(s) "
            f"in {total_batches} batches (one-time, cached afterwards)..."
        )
        try:
            for i in range(0, len(missing), BATCH_SIZE):
                batch = missing[i : i + BATCH_SIZE]
                batch_no = i // BATCH_SIZE + 1
                print(f"\r  batch {batch_no}/{total_batches}", end="", flush=True)
                try:
                    cache.update(_fetch_batch(batch))
                except requests.RequestException as e:
                    print(f"\n  ! API error on batch {batch_no}: {e}")
                if batch_no % 10 == 0:
                    _save_cache(cache, cache_path)  # checkpoint
                time.sleep(0.3)  # be polite
            print()
            # mark tracks ReccoBeats doesn't know so we don't re-ask every run
            for tid in missing:
                entry = cache.setdefault(
                    tid, {"bpm": None, "camelot": None, "energy": None}
                )
                for k in ("danceability", "valence", "acousticness"):
                    entry.setdefault(k, None)
        except KeyboardInterrupt:
            print("\n  Interrupted - progress saved, rerun to continue.")
        finally:
            _save_cache(cache, cache_path)

    if deep:
        # second pass: BPM from 30s preview audio via ReccoBeats analysis (free)
        preview_fill(tracks, cache, cache_path)
        # third pass: SoundStat fills key+bpm for anything left (optional, needs key)
        soundstat_fill(tracks, cache, cache_path)

    not_found = []
    for t in tracks:
        feats = cache.get(t["id"], {})
        t["bpm"] = feats.get("bpm")
        t["camelot"] = feats.get("camelot")
        t["energy"] = feats.get("energy")
        t["danceability"] = feats.get("danceability")
        t["valence"] = feats.get("valence")
        t["acousticness"] = feats.get("acousticness")

        # manual overrides win
        ov = overrides.get(t["id"].lower()) or overrides.get(
            f"{t['artist']} - {t['name']}".lower()
        )
        if ov:
            if ov["bpm"]:
                t["bpm"] = ov["bpm"]
            if ov["camelot"]:
                t["camelot"] = ov["camelot"]

        if t["bpm"] is None and t["camelot"] is None:
            not_found.append(t)

    if not_found:
        missing_path = os.path.join(cache_dir, "missing_tracks.csv")
        with open(missing_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["match", "bpm", "camelot"])
            for t in not_found:
                w.writerow([f"{t['artist']} - {t['name']}", "", ""])
        print(
            f"\n! No BPM/key data found for {len(not_found)}/{len(tracks)} track(s).\n"
            f"  Full list written to missing_tracks.csv - fill in bpm/camelot for\n"
            f"  the ones you care about and copy those rows into overrides.csv.\n"
        )
    return tracks


def _reccobeats_id(spotify_id: str) -> str | None:
    """Resolve a Spotify track ID to its ReccoBeats UUID."""
    r = requests.get(RECCOBEATS_TRACK_URL, params={"ids": spotify_id}, timeout=30)
    r.raise_for_status()
    content = r.json().get("content", [])
    for item in content:
        if item.get("id"):
            return item["id"]
    return None


def reccobeats_recommend(spotify_id: str, size: int = 100) -> list[dict]:
    """Similar tracks for a seed from ReccoBeats' free recommendation API.

    The recommendation endpoint wants ReccoBeats UUIDs and a mandatory
    'size', so we resolve the Spotify ID first.
    Returns simplified track dicts (Spotify id/uri, name, artist)."""
    try:
        rb_id = _reccobeats_id(spotify_id)
        if rb_id is None:
            raise RuntimeError(
                "This track isn't in the ReccoBeats database, so no "
                "similar-song pool could be built for it."
            )
        r = requests.get(
            RECCOBEATS_RECO_URL,
            params={"seeds": rb_id, "size": min(max(size, 1), 100)},
            timeout=30,
        )
        if r.status_code == 400:
            raise RuntimeError(
                f"ReccoBeats recommendation failed: HTTP 400: {r.text[:200]}"
            )
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"ReccoBeats recommendation failed: {e}") from e

    content = r.json().get("content", [])
    out, seen = [], set()
    for item in content:
        sid = _extract_spotify_id(item)
        if not sid or sid in seen or sid == spotify_id:
            continue
        seen.add(sid)
        artists = item.get("artists") or []
        if isinstance(artists, list):
            artist = ", ".join(
                a.get("name", "") for a in artists if isinstance(a, dict)
            ).strip(", ")
        else:
            artist = str(artists)
        out.append({
            "id": sid,
            "name": item.get("trackTitle") or item.get("name") or "?",
            "artist": artist or "?",
            "uri": f"spotify:track:{sid}",
            "preview_url": None,
        })
    return out


_FEATURE_KEYS = ("bpm", "camelot", "energy", "danceability", "valence", "acousticness")


def enrich_from_alias(track: dict, alias_ids: list[str], cache_dir: str = ".") -> bool:
    """Copy features from an alternate release of the same recording.

    The playlist still gets the user's original track; only the BPM/key data
    comes from the alias. Cached under the original track id."""
    if not alias_ids:
        return False
    cache_path = os.path.join(cache_dir, CACHE_FILE)
    cache = _load_cache(cache_path)

    def _usable(e):
        return e and e.get("bpm") and e.get("camelot")

    feats = next((cache[a] for a in alias_ids if _usable(cache.get(a))), None)
    if feats is None:
        try:
            cache.update(_fetch_batch(alias_ids[:BATCH_SIZE]))
        except requests.RequestException:
            return False
        feats = next((cache[a] for a in alias_ids if _usable(cache.get(a))), None)
    if feats is None:
        return False

    entry = {k: feats.get(k) for k in _FEATURE_KEYS}
    entry["alias"] = True
    cache[track["id"]] = entry
    _save_cache(cache, cache_path)
    for k in _FEATURE_KEYS:
        track[k] = entry[k]
    print(f"  Matched '{track['name']}' to an alternate release - BPM/key copied.")
    return True


def deep_enrich_one(track: dict, cache_dir: str = ".") -> dict:
    """Analyze a single track's audio (preview + SoundStat if configured).
    Used for seed tracks that aren't in the ReccoBeats database."""
    cache_path = os.path.join(cache_dir, CACHE_FILE)
    cache = _load_cache(cache_path)
    # allow a retry even if a previous bulk run marked it tried
    entry = cache.get(track["id"])
    if entry:
        entry.pop("preview_tried", None)
        entry.pop("soundstat_tried", None)
    preview_fill([track], cache, cache_path)
    soundstat_fill([track], cache, cache_path)
    feats = cache.get(track["id"], {})
    for k in ("bpm", "camelot", "energy", "danceability", "valence", "acousticness"):
        track[k] = track.get(k) or feats.get(k)
    return track
