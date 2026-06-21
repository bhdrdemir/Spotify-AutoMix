"""Spotify Web API wrapper (spotipy)."""

import os

import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private user-library-read"
)


def get_client() -> spotipy.Spotify:
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing Spotify credentials.\n"
            "Copy .env.example to .env and fill in SPOTIFY_CLIENT_ID / "
            "SPOTIFY_CLIENT_SECRET (see README for how to create them)."
        )
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            open_browser=True,
        ),
        retries=0,          # never silently sleep for hours on rate limits
        status_retries=0,
        requests_timeout=30,
    )


def _rate_limit_minutes(e: "spotipy.SpotifyException") -> int:
    try:
        return max(1, int(e.headers.get("Retry-After", 60)) // 60)
    except Exception:  # noqa: BLE001
        return 60


def _simplify(item: dict) -> dict | None:
    track = item.get("track") or item
    if not track or not track.get("id"):
        return None  # local files / unavailable tracks
    return {
        "id": track["id"],
        "name": track["name"],
        "artist": ", ".join(a["name"] for a in track["artists"]),
        "artist_ids": [a["id"] for a in track["artists"] if a.get("id")],
        "artist_names": [a["name"] for a in track["artists"] if a.get("id")],
        "uri": track["uri"],
        "preview_url": track.get("preview_url"),
    }


TRACKS_CACHE_FILE = "tracks_cache.json"
TRACKS_CACHE_TTL = 6 * 3600  # refetch from Spotify after 6 hours


def _source_key(source: str) -> str:
    if source.lower() in ("liked", "saved"):
        return "liked"
    import re as _re
    m = _re.search(r"playlist[/:]([A-Za-z0-9]+)", source)
    return m.group(1) if m else source


def _load_tracks_cache(cache_dir: str = ".") -> dict:
    import json
    path = os.path.join(cache_dir, TRACKS_CACHE_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_tracks_cache(data: dict, cache_dir: str = ".") -> None:
    import json
    with open(os.path.join(cache_dir, TRACKS_CACHE_FILE), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def get_tracks(sp: spotipy.Spotify, source: str, refresh: bool = False,
               cache_dir: str = ".") -> list[dict]:
    """source: a playlist URL/URI/ID, or 'liked' for the user's saved tracks.

    The track list is cached locally (tracks_cache.json) so reloading doesn't
    hammer the Spotify API. refresh=True forces a refetch."""
    import time as _time

    key = _source_key(source)
    store = _load_tracks_cache(cache_dir)
    entry = store.get(key)
    fresh = entry and (_time.time() - entry.get("ts", 0)) < TRACKS_CACHE_TTL
    if entry and fresh and not refresh:
        print(f"Using locally cached track list ({len(entry['tracks'])} tracks).")
        return entry["tracks"]

    tracks: list[dict] = []
    raw_count = 0
    skipped = 0
    try:
        if key == "liked":
            results = sp.current_user_saved_tracks(limit=50)
        else:
            results = sp.playlist_items(source, limit=100, additional_types=("track",))
        while results:
            for item in results["items"]:
                raw_count += 1
                t = _simplify(item)
                if t:
                    tracks.append(t)
                else:
                    skipped += 1
            results = sp.next(results) if results.get("next") else None
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            if entry:  # stale cache beats no data
                print(
                    "  ! Spotify rate limit hit - using the locally cached "
                    f"track list ({len(entry['tracks'])} tracks) instead."
                )
                return entry["tracks"]
            raise SystemExit(
                "Spotify rate limit reached. Try again in about "
                f"{_rate_limit_minutes(e)} minute(s).\n"
                "Tip: creating a fresh app at developer.spotify.com/dashboard "
                "and putting its keys in .env gives you a fresh quota instantly."
            ) from e
        if e.http_status == 404:
            raise SystemExit(
                "Playlist not found (404). Note: since Nov 2024 the Spotify API "
                "blocks Spotify-generated playlists (Blend, Mix, editorial) for "
                "new apps. Use a playlist you created yourself, or copy the "
                "tracks into a new playlist first."
            ) from e
        raise

    if skipped:
        print(f"  ! {skipped}/{raw_count} item(s) skipped (local files or unavailable tracks)")
    if raw_count == 0:
        print(
            "  ! Spotify returned 0 items for this playlist. This usually means the\n"
            "    playlist is Spotify-generated (Blend/Mix/editorial), which the API\n"
            "    blocks for new apps. Copy the songs into your own playlist and retry."
        )

    # dedupe, keep order
    seen, unique = set(), []
    for t in tracks:
        if t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)

    if unique:
        store[key] = {"ts": _time.time(), "tracks": unique}
        _save_tracks_cache(store, cache_dir)
    return unique


def _itunes_genre(artist_name: str) -> list[str]:
    """Artist's primary genre from the free iTunes Search API (no key needed)."""
    import requests as _rq

    try:
        r = _rq.get(
            "https://itunes.apple.com/search",
            params={"term": artist_name, "entity": "musicArtist", "limit": 1},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if results and results[0].get("primaryGenreName"):
            g = results[0]["primaryGenreName"].lower().replace("/", " ")
            return [g]
    except Exception:  # noqa: BLE001
        pass
    return []


_GENRE_NAME_CACHE = "genre_name_cache.json"


def attach_genres_by_name(tracks: list[dict], cache_dir: str = ".",
                          log=print) -> None:
    """Attach a 'genres' token set to each track using the artist name and the
    free iTunes genre database (no key, cached). Used for the discover pool
    where we only have artist names, not Spotify IDs."""
    import json

    path = os.path.join(cache_dir, _GENRE_NAME_CACHE)
    cache = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    names = []
    seen = set()
    for t in tracks:
        primary = (t.get("artist") or "").split(",")[0].strip().lower()
        t["_artist_key"] = primary
        if primary and primary not in seen:
            seen.add(primary)
            names.append(primary)

    missing = [n for n in names if n not in cache]
    if missing:
        from concurrent.futures import ThreadPoolExecutor

        log(f"Matching genres for {len(missing)} artist(s)...")
        done = 0
        # low concurrency: iTunes throttles aggressively and returns empty
        # results under heavy parallelism (which silently breaks the gate)
        with ThreadPoolExecutor(max_workers=3) as ex:
            for n, genres in zip(missing, ex.map(_itunes_genre, missing)):
                cache[n] = genres
                done += 1
                if done % 20 == 0 or done == len(missing):
                    log(f"  genres {done}/{len(missing)}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    for t in tracks:
        toks = set()
        for g in cache.get(t.get("_artist_key", ""), []):
            toks.update(g.split())
        t["genre_tokens"] = toks


def genres_match(seed_tokens: set, cand_tokens: set) -> bool:
    """Fail-closed gate: when the seed's genre is known, a candidate must
    share a genre token. Candidates with unknown genre are dropped. Only when
    the seed genre itself is unknown do we let everything through (last resort)."""
    if not seed_tokens:
        return True  # no reference to filter against
    return bool(cand_tokens and (seed_tokens & cand_tokens))


_SPOTIFY_GENRE_CACHE = "spotify_genre_cache.json"


def spotify_artist_genres(sp, names: list[str], cache_dir: str = ".",
                          log=print) -> tuple[dict, bool]:
    """Fine-grained genres from Spotify's artist search (e.g. 'k-pop',
    'australian psychedelic rock') - far more precise than iTunes. The bulk
    /artists endpoint is blocked for new apps, but artist *search* works.
    Returns ({name_lower: [genres]}, blocked)."""
    import json
    import time as _time

    path = os.path.join(cache_dir, _SPOTIFY_GENRE_CACHE)
    cache = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    missing = [n for n in names if n and n not in cache]
    blocked = False
    if missing:
        log(f"Looking up genres for {len(missing)} artist(s)...")
        for i, n in enumerate(missing, 1):
            try:
                res = sp.search(q=n, type="artist", limit=1)
                items = (res.get("artists") or {}).get("items") or []
                cache[n] = [g.lower() for g in (items[0].get("genres") or [])] if items else []
            except spotipy.SpotifyException as e:
                if e.http_status in (401, 403):
                    blocked = True
                    break
                cache[n] = []
            except Exception:  # noqa: BLE001
                cache[n] = []
            if i % 20 == 0 or i == len(missing):
                log(f"  genres {i}/{len(missing)}")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
            _time.sleep(0.05)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    return {n: cache.get(n, []) for n in names}, blocked


def _tokens(genre_list: list[str]) -> set:
    toks = set()
    for g in genre_list:
        toks.update(g.split())
    return toks


def attach_genres_smart(sp, tracks: list[dict], cache_dir: str = ".",
                        log=print) -> None:
    """Attach 'genre_tokens' to each track. Prefers Spotify's fine genres;
    falls back to iTunes if Spotify search is blocked or empty."""
    names, seen = [], set()
    for t in tracks:
        primary = (t.get("artist") or "").split(",")[0].strip().lower()
        t["_artist_key"] = primary
        if primary and primary not in seen:
            seen.add(primary); names.append(primary)

    by_name, blocked = spotify_artist_genres(sp, names, cache_dir, log)
    if blocked or not any(by_name.values()):
        log("Spotify genres unavailable - falling back to iTunes.")
        attach_genres_by_name(tracks, cache_dir, log)
        return
    for t in tracks:
        t["genre_tokens"] = _tokens(by_name.get(t.get("_artist_key", ""), []))


def add_genres(sp: spotipy.Spotify, tracks: list[dict], cache_dir: str = ".") -> None:
    """Attach a 'genres' list to each track (union of its artists' genres).

    Tries the Spotify artists endpoint first; if Spotify blocks it (403 on
    some new apps), falls back to the free iTunes Search API (one genre per
    artist, no key needed). Cached in genre_cache.json either way."""
    import json
    import os
    import time as _time

    cache_path = os.path.join(cache_dir, "genre_cache.json")
    cache: dict = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    def save():
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    id_to_name = {}
    for t in tracks:
        for aid, aname in zip(t.get("artist_ids", []), t.get("artist_names", [])):
            id_to_name[aid] = aname

    missing = [aid for aid in id_to_name if aid not in cache]
    if missing:
        import requests as _rq

        print(f"Fetching genres for {len(missing)} artist(s)...")
        spotify_blocked = False
        fetched = set()
        token = sp.auth_manager.get_access_token(as_dict=False)
        total_batches = (len(missing) + 49) // 50
        for i in range(0, len(missing), 50):
            batch = missing[i : i + 50]
            print(f"\r  batch {i // 50 + 1}/{total_batches}", end="", flush=True)
            try:
                # direct call: spotipy builds 'artists/?ids=' (trailing slash)
                # which some Spotify gateways reject with 403
                r = _rq.get(
                    "https://api.spotify.com/v1/artists",
                    params={"ids": ",".join(batch)},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                )
                if r.status_code in (401, 403):
                    spotify_blocked = True
                    print(
                        "\n  ! Spotify blocked the artists endpoint - using "
                        "the free iTunes genre database instead..."
                    )
                    break
                r.raise_for_status()
                for artist in r.json().get("artists", []):
                    if artist:
                        cache[artist["id"]] = artist.get("genres", [])
                        fetched.add(artist["id"])
                if (i // 50 + 1) % 10 == 0:
                    save()
            except _rq.RequestException as e:
                print(f"\n  ! genre fetch error: {e}")
                break
        print()
        save()

        if spotify_blocked:
            todo = [aid for aid in missing if aid not in fetched]
            done = 0
            try:
                for aid in todo:
                    done += 1
                    print(f"\r  {done}/{len(todo)} artists", end="", flush=True)
                    cache[aid] = _itunes_genre(id_to_name[aid])
                    if done % 25 == 0:
                        save()
                    _time.sleep(0.25)  # stay under iTunes rate limits
                print()
            except KeyboardInterrupt:
                print("\n  Interrupted - genre progress saved.")
            finally:
                save()

    for t in tracks:
        genres: list[str] = []
        for aid in t.get("artist_ids", []):
            genres.extend(cache.get(aid, []))
        t["genres"] = sorted(set(genres))


_VERSION_WORDS = (
    "slowed", "sped", "speed up", "nightcore", "reverb", "remix",
    "acoustic", "live", "instrumental", "8d", "cover", "mashup",
)


def find_alias_ids(sp: spotipy.Spotify, track: dict, limit: int = 8) -> list[str]:
    """Find alternate Spotify releases of the same recording (album vs single
    vs remaster). Used to fetch BPM/key when the user's exact pressing isn't
    in the database. Version-modifier words must match (a 'slowed' track is
    never matched to the normal version - different BPM/key)."""
    name = (track.get("name") or "").replace('"', "")
    artist = (track.get("artist") or "").split(",")[0].replace('"', "").strip()
    if not name:
        return []
    try:
        res = sp.search(q=f'track:"{name}" artist:"{artist}"', type="track", limit=limit)
    except spotipy.SpotifyException:
        return []
    items = (res.get("tracks") or {}).get("items") or []
    orig = name.lower()
    out = []
    for t in items:
        if not t or not t.get("id") or t["id"] == track.get("id"):
            continue
        cand = (t.get("name") or "").lower()
        if any((w in orig) != (w in cand) for w in _VERSION_WORDS):
            continue
        out.append(t["id"])
    return out


def create_playlist(sp: spotipy.Spotify, name: str, uris: list[str], description: str = "") -> str:
    import requests as _rq

    # /me/playlists works where /users/{id}/playlists sometimes 403s
    token = sp.auth_manager.get_access_token(as_dict=False)
    r = _rq.post(
        "https://api.spotify.com/v1/me/playlists",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": name, "public": False, "description": description},
        timeout=30,
    )
    if r.status_code == 429:
        mins = max(1, int(r.headers.get("Retry-After", 60)) // 60)
        raise SystemExit(
            f"Spotify rate limit reached - try again in ~{mins} minute(s)."
        )
    if r.status_code == 403:
        raise SystemExit(
            "Spotify refused to create the playlist (403).\n"
            "Fixes to try:\n"
            "  1. Delete the '.cache' file in this folder and rerun "
            "(forces a fresh login with all permissions).\n"
            "  2. In your app at developer.spotify.com/dashboard -> "
            "Settings, make sure 'Web API' is enabled.\n"
            "  3. Make sure you logged in with the SAME account that "
            "owns the app (or add the account under User Management)."
        )
    r.raise_for_status()
    playlist = r.json()
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist["id"], uris[i : i + 100])
    return playlist["external_urls"]["spotify"]
