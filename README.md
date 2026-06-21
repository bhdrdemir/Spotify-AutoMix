# Spotify AutoMix

**Build harmonically mixed Spotify playlists automatically — DJ-style sets where every song flows into the next.**

AutoMix orders songs by **musical key (Camelot), tempo (BPM), and sound character**, so transitions are smooth instead of jarring — the same idea DJs use when beat-matching a set. Pick a song or a playlist, and AutoMix returns a sequence where each track is the best harmonic match for the one before it, then saves it straight to your Spotify account.

It runs entirely on your own machine and uses only free APIs (no paid services required).

---

## What it does

- **Any song → a mix.** Search any track on Spotify and get a playlist of similar-genre songs that share its key and tempo. No playlist needed.
- **My playlist → seed mix.** Load one of your playlists (or your Liked Songs), pick a song, and mix around it using only your own library.
- **Sort a playlist.** Reorder an entire playlist into the smoothest possible DJ flow.
- **Saves to Spotify.** One click creates the playlist in your account, ready to play (great with Apple Music / Spotify crossfade or AutoMix).

A clean pink-and-purple web UI shows BPM, key, and a "flow score" for every transition, with live progress while it works.

---

## How it works

1. **Track data.** Spotify deprecated its public audio-features endpoint in late 2024, so AutoMix gets each song's **tempo, key, and audio features** from the free [ReccoBeats](https://reccobeats.com) API. Missing tracks are filled in by analysing the song's 30-second preview, or via alternate releases of the same recording. Everything is cached locally so repeat runs are instant.

2. **Camelot conversion.** Each track's key + mode is converted to a **Camelot code** (e.g. `8A`, `11B`) — the wheel DJs use to know which keys mix well together.

3. **Transition scoring (0–100).** Every possible song-to-song transition is scored as a weighted blend:
   - **Key — 45%:** same key = 100 · ±1 on the wheel = 90 · relative major/minor = 85 · energy boost = 70.
   - **Tempo — 35%:** within ~2% = 100, fading out by ~6%; half/double tempo (e.g. 70 ↔ 140 BPM) also counts.
   - **Vibe — 20%:** similarity of energy, danceability, valence, and acousticness — keeps unrelated styles from clashing even when key and tempo line up.

4. **Genre gate (discover mode).** Candidate songs are filtered to the seed's genre family using Spotify's fine-grained genres (with an iTunes fallback), so a mix stays stylistically coherent.

5. **Chained sequencing.** Starting from the seed, AutoMix repeatedly appends the best-matching *next* song — so the playlist flows track-to-track like a real set, rather than every song just matching the first one.

---

## Setup

Requires **Python 3.10+**.

```bash
git clone https://github.com/<your-username>/spotify-automix.git
cd spotify-automix
pip install -r requirements.txt
```

### Get your Spotify API keys (free, ~2 minutes)

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in with your normal Spotify account.
2. Click **Create app** — give it any name and description.
3. Set the **Redirect URI** to `http://127.0.0.1:8888/callback`.
4. Copy your **Client ID** and **Client Secret**.
5. Copy the example env file and paste your keys in:

   ```bash
   cp .env.example .env
   ```

   Then edit `.env` with your `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

The first run opens a browser once so you can authorise your account.

> **Note:** As of 2026, new Spotify apps run in development mode and are limited to a small number of users that you add yourself in the dashboard under **User Management**. This is fine for personal use — each user just creates their own free app.

---

## Usage

### Web UI (recommended)

```bash
python ui/app.py
```

Open **http://127.0.0.1:7777**, then either:

- **Any song** — search any track and hit *Create my mix*.
- **My playlist** — load a playlist or type `liked`, pick a song to mix around, or *Sort whole playlist*.

Choose a length (2–50 songs), preview the mix with its flow scores, and click **Save to Spotify**.

### Command line

```bash
# Mix an entire playlist
python mixer.py "https://open.spotify.com/playlist/XXXX"

# Build a mix around one song
python mixer.py "https://open.spotify.com/playlist/XXXX" --seed "song name" --count 15

# Use your Liked Songs as the pool
python mixer.py liked --seed "daft punk"

# Just print the order without creating a playlist
python mixer.py "https://open.spotify.com/playlist/XXXX" --dry-run
```

---

## Manual data (overrides)

If ReccoBeats doesn't know a track, or you want to correct its data, create an `overrides.csv` (see `overrides.csv.example`):

```csv
match,bpm,camelot
Daft Punk - One More Time,123,8B
4uLU6hMCjMI75M1A2tKUQC,170,2A
```

`match` is either a Spotify track ID or a case-insensitive `Artist - Title` string. Overrides always win over API data.

### Optional: better coverage with SoundStat

For tracks ReccoBeats can't identify, you can add a free [SoundStat](https://soundstat.info) API key as `SOUNDSTAT_KEY` in your `.env`. It analyses missing songs on demand and also returns their key.

---

## Project structure

| File | Purpose |
|------|---------|
| `ui/app.py` | Web UI + local server |
| `mixer.py` | Command-line interface |
| `camelot.py` | Key/mode → Camelot codes and harmonic scoring |
| `sequencer.py` | Transition scoring and playlist ordering |
| `features.py` | Track data: ReccoBeats, preview analysis, caching, overrides |
| `spotify_client.py` | Spotify Web API wrapper and genre lookup |

---

## Notes & limitations

- **Key/BPM values come from ReccoBeats, not Spotify**, so they're estimates and may differ from other tools — but they're internally consistent, which is what matters for mixing.
- Recommendation quality for the *Any song* mode depends on ReccoBeats' database; mainstream tracks give the best pools.
- Spotify-generated playlists (Blend, Daily Mix, editorial) can't be read via the API — copy the songs into your own playlist first.
- All data and credentials stay on your machine; nothing is uploaded anywhere.

---

## License

MIT — free to use, modify, and share.
