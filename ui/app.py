#!/usr/bin/env python3
"""spotify-automix web UI.

Run from the project root (or anywhere):
    python ui/app.py
then open http://127.0.0.1:7777
"""

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # so .env, cache.json, overrides.csv resolve correctly

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import logging

import spotipy
from flask import Flask, jsonify, request, Response

# keep the terminal clean: hide per-request access log lines
logging.getLogger("werkzeug").setLevel(logging.ERROR)

from features import (
    enrich_tracks, deep_enrich_one, reccobeats_recommend, enrich_from_alias,
)
from camelot import key_score
from sequencer import seed_mix, order_tracks, transition_score, bpm_score
from spotify_client import (
    get_client, get_tracks, create_playlist, find_alias_ids,
    attach_genres_smart, genres_match,
)

app = Flask(__name__)

STATE = {"tracks": [], "sp": None}
JOB = {"running": False, "kind": None, "chunks": [], "result": None, "error": None}
LOCK = threading.Lock()

MIN_MIX, MAX_MIX = 2, 50
MAX_SORT = 800


class _Tee:
    def __init__(self, orig, chunks):
        self.orig, self.chunks = orig, chunks

    def write(self, s):
        self.orig.write(s); self.orig.flush(); self.chunks.append(s)

    def flush(self):
        self.orig.flush()


def _sp():
    if STATE["sp"] is None:
        STATE["sp"] = get_client()
    return STATE["sp"]


def _track_json(t, score=None):
    return {
        "id": t["id"], "name": t["name"], "artist": t["artist"], "uri": t["uri"],
        "bpm": t.get("bpm"), "camelot": t.get("camelot"),
        "energy": t.get("energy"), "score": score,
    }


def _has_data(t):
    return t.get("bpm") or t.get("camelot")


def _fill_seed_data(seed):
    if _has_data(seed):
        return
    try:
        alias = find_alias_ids(_sp(), seed)
    except Exception:  # noqa: BLE001
        alias = []
    if alias:
        enrich_from_alias(seed, alias)
    if not _has_data(seed):
        deep_enrich_one(seed)


def _run_job(kind, fn):
    """Run fn() in a thread, capturing stdout for the live progress panel."""
    JOB.update(running=True, kind=kind, result=None, error=None)
    JOB["chunks"].clear()
    old = sys.stdout
    sys.stdout = _Tee(old, JOB["chunks"])
    try:
        JOB["result"] = fn()
    except SystemExit as e:
        JOB["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        JOB["error"] = f"{type(e).__name__}: {e}"
    finally:
        sys.stdout = old
        JOB["running"] = False


def _start(kind, fn):
    if JOB["running"]:
        return jsonify({"error": "Something is already running, please wait."}), 409
    threading.Thread(target=_run_job, args=(kind, fn), daemon=True).start()
    return jsonify({"started": True, "kind": kind})


# ---------------------------------------------------------------- load
def _job_load(source, refresh):
    sp = _sp()
    print("Fetching tracks...")
    tracks = get_tracks(sp, source, refresh=refresh)
    if not tracks:
        raise SystemExit(
            "Spotify returned no tracks. If this is a Spotify-made playlist "
            "(Blend/Mix), copy the songs into your own playlist first."
        )
    print(f"Found {len(tracks)} track(s).")
    tracks = enrich_tracks(tracks)
    STATE["tracks"] = tracks
    known = [t for t in tracks if _has_data(t)]
    print("Ready.")
    return {
        "view": "library",
        "tracks": [_track_json(t) for t in tracks],
        "total": len(tracks), "with_data": len(known),
    }


@app.post("/api/load")
def api_load():
    data = request.get_json(force=True)
    source = (data.get("source") or "").strip()
    if not source:
        return jsonify({"error": "Enter a playlist URL or 'liked'."}), 400
    return _start("load", lambda: _job_load(source, bool(data.get("refresh"))))


# ---------------------------------------------------------------- discover
def _job_discover(seed, count):
    seed_track = {
        "id": seed["id"], "name": seed.get("name") or "?",
        "artist": seed.get("artist") or "?",
        "uri": seed.get("uri") or f"spotify:track:{seed['id']}",
        "preview_url": None,
    }
    print(f"Finding songs similar to '{seed_track['name']}'...")
    recs = reccobeats_recommend(seed_track["id"], size=100)
    seen = {seed_track["id"]}
    pool = []
    for r in recs:
        if r["id"] not in seen:
            seen.add(r["id"]); pool.append(r)
    # ReccoBeats ranks recommendations best-first; the long tail is noisy
    # (obscure / unrelated uploads), so keep only the top of the list.
    pool = pool[:60]

    tracks = [seed_track] + pool
    enrich_tracks(tracks)
    _fill_seed_data(seed_track)
    if not _has_data(seed_track):
        raise SystemExit("Couldn't find BPM/key data for this song.")

    # 1) hard musical filter (free, local): keep songs whose key is
    #    harmonically compatible with the seed and whose tempo is close.
    sc, sb = seed_track.get("camelot"), seed_track.get("bpm")
    strong = [
        t for t in pool
        if _has_data(t)
        and key_score(sc, t.get("camelot")) >= 70      # same/adjacent/relative/+2
        and bpm_score(sb, t.get("bpm")) >= 55          # within tempo window
    ]
    if len(strong) < 2:  # relax if the seed's data is sparse
        strong = [t for t in pool if _has_data(t)]
    if not strong:
        raise SystemExit(
            "No songs with usable key/tempo data were found for this seed."
        )
    print(f"{len(strong)} song(s) match the key & tempo; checking genres...")

    # 2) genre gate using Spotify's fine genres (only on the survivors, so
    #    few lookups). Fail-closed: unknown-genre candidates are dropped.
    attach_genres_smart(_sp(), [seed_track] + strong)
    seed_tok = seed_track.get("genre_tokens") or set()
    genre_pass = [t for t in strong
                  if genres_match(seed_tok, t.get("genre_tokens") or set())]
    dropped = len(strong) - len(genre_pass)

    # graceful fallback: only enforce the genre gate when it leaves enough
    # songs. If the similar-song pool is thin (niche seed), keep the key/tempo
    # matched pool so the user still gets a coherent mix instead of an error.
    target = min(count, 8)
    if len(genre_pass) >= max(2, target // 2):
        candidates = genre_pass
        if dropped:
            print(f"Filtered out {dropped} song(s) from other genres.")
    else:
        candidates = strong
        print("Not enough same-genre songs in the pool - matching on "
              "key & tempo only for this seed.")

    # 3) sequence within the filtered pool (chained: each next matches prev)
    print("Sequencing the mix...")
    chain = seed_mix([seed_track] + candidates, seed_track, count=count)

    print("Done.")
    scores = [s for _, s in chain if s is not None]
    return {
        "view": "mix",
        "chain": [_track_json(t, s) for t, s in chain],
        "avg": round(sum(scores) / len(scores), 1) if scores else 0,
        "pool": len(candidates), "seedName": seed_track["name"],
    }


@app.post("/api/discover")
def api_discover():
    data = request.get_json(force=True)
    seed = data.get("seed") or {}
    count = max(MIN_MIX, min(int(data.get("count") or 12), MAX_MIX))
    if not seed.get("id"):
        return jsonify({"error": "Pick a song first."}), 400
    return _start("mix", lambda: _job_discover(seed, count))


# ---------------------------------------------------------------- library mix / sort
def _job_mix(ids, seed_id, count, sort_all):
    pool = [t for t in STATE["tracks"] if not ids or t["id"] in ids]
    pool = [t for t in pool if _has_data(t) or t["id"] == seed_id]

    if seed_id:
        seed = next((t for t in pool if t["id"] == seed_id), None)
        if seed is None:
            seed = next((t for t in STATE["tracks"] if t["id"] == seed_id), None)
            if seed is None:
                raise SystemExit("Seed track not found.")
            pool.append(seed)
        if not _has_data(seed):
            print("Looking up this song's data...")
            _fill_seed_data(seed)
        if not _has_data(seed):
            raise SystemExit("Couldn't find BPM/key data for this song.")
        if len(pool) < 2:
            raise SystemExit("Not enough songs with data in the pool.")
        chain = seed_mix(pool, seed, count=count)
        seed_name = seed["name"]
    else:
        if len(pool) < 2:
            raise SystemExit("Need at least 2 songs with data.")
        if len(pool) > MAX_SORT:
            raise SystemExit(
                f"Sorting {len(pool)} songs at once is too slow. Works best up "
                f"to {MAX_SORT}; load a single playlist instead of everything."
            )
        print(f"Sorting {len(pool)} songs into the smoothest order...")
        chain = order_tracks(pool)
        seed_name = None

    scores = [s for _, s in chain if s is not None]
    return {
        "view": "mix",
        "chain": [_track_json(t, s) for t, s in chain],
        "avg": round(sum(scores) / len(scores), 1) if scores else 0,
        "seedName": seed_name,
    }


@app.post("/api/mix")
def api_mix():
    data = request.get_json(force=True)
    seed_id = data.get("seed_id")
    count = max(MIN_MIX, min(int(data.get("count") or 12), MAX_MIX))
    sort_all = bool(data.get("sort_all"))
    ids = data.get("ids") or []
    return _start("mix", lambda: _job_mix(set(ids), seed_id, count, sort_all))


# ---------------------------------------------------------------- status / create
@app.get("/api/status")
def api_status():
    text = "".join(JOB["chunks"])
    lines = [ln.split("\r")[-1] for ln in text.split("\n")]
    tail = [ln for ln in lines if ln.strip()][-8:]
    out = {"running": JOB["running"], "kind": JOB["kind"], "log": "\n".join(tail)}
    if not JOB["running"]:
        if JOB["error"]:
            out["error"] = JOB["error"]
        elif JOB["result"]:
            out["result"] = JOB["result"]
    return jsonify(out)


@app.post("/api/create")
def api_create():
    data = request.get_json(force=True)
    name = (data.get("name") or "AutoMix").strip() or "AutoMix"
    uris = data.get("uris") or []
    if len(uris) < 2:
        return jsonify({"error": "Nothing to create."}), 400
    try:
        url = create_playlist(_sp(), name, uris,
                              description="Harmonic auto-mix by spotify-automix")
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"url": url})


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoMix</title>
<style>
  :root{
    --bg:#0e0b14; --panel:#181222; --panel2:#221a30; --text:#f5f3f7;
    --muted:#9b93a8; --pink:#f472b6; --purple:#a855f7;
    --grad:linear-gradient(90deg,#f472b6,#a855f7); --green:#1db954;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:radial-gradient(1200px 600px at 80% -10%,#2a1440 0%,var(--bg) 55%);
    color:var(--text);font-family:'Segoe UI','Helvetica Neue',Arial,sans-serif;min-height:100vh}
  .wrap{max-width:780px;margin:0 auto;padding:30px 20px 80px}
  header{display:flex;align-items:center;gap:14px;margin-bottom:26px}
  .logo{width:46px;height:46px;border-radius:50%;background:var(--grad);display:flex;
    align-items:center;justify-content:center;font-size:23px;color:#fff;
    box-shadow:0 0 24px rgba(168,85,247,.45)}
  h1{font-size:27px;font-weight:700;letter-spacing:-.5px}
  h1 span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
  .sub{color:var(--muted);font-size:13px;margin-top:2px}
  .card{background:var(--panel);border:1px solid #2c2240;border-radius:16px;padding:22px;margin-bottom:18px}
  .ttl{font-size:17px;font-weight:700;margin-bottom:14px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  input[type=text]{background:var(--panel2);color:var(--text);border:1px solid #382a52;
    border-radius:500px;padding:13px 18px;font-size:14px;outline:none;transition:border-color .15s}
  input[type=text]:focus{border-color:var(--purple)}
  .btn{border:none;border-radius:500px;cursor:pointer;padding:13px 28px;font-size:14px;
    font-weight:700;color:#fff;background:var(--grad);transition:transform .1s,box-shadow .15s}
  .btn:hover{transform:scale(1.04);box-shadow:0 4px 18px rgba(244,114,182,.35)}
  .btn:disabled{opacity:.4;cursor:default;transform:none;box-shadow:none}
  .btn.ghost{background:transparent;border:1px solid #4b3a6b;color:var(--text);font-weight:600}
  .btn.green{background:var(--green)}
  .btn.big{font-size:15px;padding:15px 36px}
  .hint{color:var(--muted);font-size:12.5px;margin-top:10px;line-height:1.5}
  .error{background:#3b1530;border:1px solid #a83267;color:#ffb3d2;border-radius:10px;
    padding:12px 16px;margin-top:12px;font-size:13.5px;display:none;white-space:pre-wrap}
  .tabs{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
  .tab{border:1px solid #4b3a6b;background:transparent;color:var(--muted);border-radius:500px;
    padding:9px 20px;font-size:13.5px;font-weight:700;cursor:pointer}
  .tab.on{background:var(--grad);border-color:transparent;color:#fff}
  .searchwrap{position:relative;flex:1;min-width:240px}
  .searchwrap input{width:100%}
  #source{flex:1;min-width:240px}
  .dropdown{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:30;background:#1d1529;
    border:1px solid #3a2b55;border-radius:12px;max-height:300px;overflow-y:auto;display:none;
    box-shadow:0 12px 32px rgba(0,0,0,.5)}
  .opt{padding:10px 14px;cursor:pointer;display:flex;justify-content:space-between;gap:10px;align-items:center}
  .opt:hover{background:#2b2040}
  .opt .n{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .opt .a{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .seedcard{display:none;margin-top:14px;padding:14px 16px;background:linear-gradient(135deg,#2b1438,#231a33);
    border:1px solid #4b3a6b;border-radius:12px;align-items:center;gap:14px}
  .seedcard.show{display:flex}
  .seedinfo{flex:1;min-width:0}
  .seedname{font-weight:700}
  .seedartist{color:var(--muted);font-size:13px}
  .lenrow{display:flex;align-items:center;gap:14px;margin-top:18px;flex-wrap:wrap}
  input[type=range]{flex:1;min-width:160px;accent-color:var(--purple)}
  .lenval{font-weight:800;font-size:16px;min-width:74px}
  .lenval b{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
  .progress{display:none;margin-top:16px}
  .bar{height:8px;border-radius:6px;background:#251c38;overflow:hidden}
  .bar > div{height:100%;width:35%;border-radius:6px;background:var(--grad);
    animation:slide 1.3s ease-in-out infinite}
  @keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
  .plog{margin-top:10px;font-family:Consolas,'Courier New',monospace;font-size:12px;
    color:#c9b8e8;white-space:pre-wrap;line-height:1.55;max-height:130px;overflow-y:auto}
  .chip{display:inline-block;padding:2px 9px;border-radius:500px;font-size:11px;font-weight:700}
  .chip.bpm{background:#2a1f3f;color:#d8b4fe}
  .chip.key{background:#3b1f33;color:#f9a8d4}
  .spinner{width:18px;height:18px;border-radius:50%;border:3px solid #3a2b55;
    border-top-color:var(--pink);animation:spin .8s linear infinite;display:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.8px;
    padding:6px 10px;border-bottom:1px solid #2c2240}
  td{padding:9px 10px;font-size:13.5px;border-bottom:1px solid #211936}
  .tname{font-weight:600}
  .tartist{color:var(--muted);font-size:12.5px}
  .num{color:var(--muted);font-size:12px;width:28px}
  .score{font-weight:800}
  .s-great{color:#4ade80}.s-ok{color:#facc15}.s-bad{color:#f87171}
  #rlink a{color:var(--pink);font-weight:700}
  .stats{color:var(--muted);font-size:13px;font-weight:400}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">&#9835;</div>
    <div><h1>Auto<span>Mix</span></h1>
    <div class="sub">DJ-style playlists that flow &middot; key + tempo + vibe</div></div>
  </header>

  <div class="card">
    <div class="tabs">
      <button class="tab on" id="tab-any" onclick="setMode('any')">Any song</button>
      <button class="tab" id="tab-pl" onclick="setMode('pl')">My playlist</button>
    </div>

    <!-- ANY SONG -->
    <div id="mode-any">
      <div class="hint" style="margin:0 0 12px">Search any song on Spotify - we build a playlist of similar-genre songs that mix smoothly with it.</div>
      <div class="row">
        <div class="searchwrap">
          <input type="text" id="anysearch" placeholder="Search any song..." oninput="anyInput()" autocomplete="off">
          <div class="dropdown" id="anydd"></div>
        </div>
        <div class="spinner" id="anyspin"></div>
      </div>
      <div class="seedcard" id="anysel">
        <div class="seedinfo"><div class="seedname" id="anyname"></div><div class="seedartist" id="anyartist"></div></div>
        <button class="btn ghost" onclick="clearAny()">Change</button>
      </div>
    </div>

    <!-- MY PLAYLIST -->
    <div id="mode-pl" style="display:none">
      <div class="hint" style="margin:0 0 12px">Load a playlist (or type <b>liked</b>), then pick a song to mix around - or sort the whole thing.</div>
      <div class="row">
        <input type="text" id="source" placeholder="playlist link or 'liked'">
        <button class="btn" id="loadbtn" onclick="load()">Load</button>
        <div class="spinner" id="loadspin"></div>
      </div>
      <label class="hint" style="display:flex;align-items:center;gap:7px;cursor:pointer">
        <input type="checkbox" id="refresh" style="accent-color:#a855f7"> Refresh from Spotify
      </label>
      <div id="plpick" style="display:none">
        <div class="row" style="margin-top:14px">
          <div class="searchwrap">
            <input type="text" id="plsearch" placeholder="Search a song to mix around..." oninput="plInput()" autocomplete="off">
            <div class="dropdown" id="pldd"></div>
          </div>
          <button class="btn ghost" id="sortbtn" onclick="sortAll()">Sort whole playlist</button>
        </div>
        <div class="seedcard" id="plsel">
          <div class="seedinfo"><div class="seedname" id="plname2"></div><div class="seedartist" id="plartist"></div></div>
          <button class="btn ghost" onclick="clearPl()">Change</button>
        </div>
        <div class="stats" id="plstats" style="margin-top:10px"></div>
      </div>
    </div>

    <div class="lenrow" id="lenrow">
      <span class="stats">Length</span>
      <input type="range" id="len" min="2" max="50" value="12" oninput="lenUp()">
      <span class="lenval"><b id="lenval">12</b> songs</span>
      <button class="btn big" id="gobtn" onclick="go()" disabled>Create my mix</button>
      <div class="spinner" id="gospin"></div>
    </div>

    <div class="progress" id="progress">
      <div class="bar"><div></div></div>
      <div class="plog" id="plog"></div>
    </div>
    <div class="error" id="err"></div>
  </div>

  <div class="card" id="resultcard" style="display:none">
    <div class="ttl">Your mix <span class="stats" id="avg"></span></div>
    <table>
      <thead><tr><th></th><th>Track</th><th>BPM</th><th>Key</th><th>Flow</th></tr></thead>
      <tbody id="result"></tbody>
    </table>
    <div class="row" style="margin-top:16px">
      <input type="text" id="plnameinput" style="flex:1;min-width:220px" value="AutoMix">
      <button class="btn green" id="createbtn" onclick="createPl()">Save to Spotify</button>
      <div class="spinner" id="createspin"></div>
    </div>
    <div class="hint" id="rlink"></div>
    <div class="error" id="createerr"></div>
  </div>
</div>

<script>
let MODE='any', TRACKS=[], CHAIN=[], COUNT=12;
let ANY=null, ANYRES=[], PLSEED=null, POLLER=null, T1=null, T2=null;

const $=id=>document.getElementById(id);
const esc=s=>(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function err(id,m){const e=$(id);e.textContent=m||'';e.style.display=m?'block':'none';}
function spin(id,on){$(id).style.display=on?'block':'none';}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});const j=await r.json();if(!r.ok)throw new Error(j.error||'Failed');return j;}
function chips(t){
  const b=t.bpm?`<span class="chip bpm">${Math.round(t.bpm)} BPM</span>`:'';
  const k=t.camelot?`<span class="chip key">${t.camelot}</span>`:'';
  return b+' '+k;
}

function setMode(m){
  MODE=m;
  $('tab-any').classList.toggle('on',m==='any');
  $('tab-pl').classList.toggle('on',m==='pl');
  $('mode-any').style.display=m==='any'?'block':'none';
  $('mode-pl').style.display=m==='pl'?'block':'none';
  updateGo();
}
function lenUp(){COUNT=+$('len').value;$('lenval').textContent=COUNT;}
function updateGo(){
  $('gobtn').disabled = MODE==='any' ? !ANY : !PLSEED;
}

/* ---- generic job polling (load + mix share this) ---- */
function startProgress(){
  $('progress').style.display='block';
  $('plog').textContent='';
  POLLER=setInterval(poll,900);
}
async function poll(){
  let j; try{ j=await (await fetch('/api/status')).json(); }catch(e){return;}
  $('plog').textContent=j.log||'';
  $('plog').scrollTop=$('plog').scrollHeight;
  if(j.running) return;
  clearInterval(POLLER);
  $('progress').style.display='none';
  spin('loadspin',false); spin('gospin',false);
  $('loadbtn').disabled=false; updateGo();
  if(j.error){ err('err',j.error); return; }
  if(!j.result) return;
  if(j.result.view==='library') onLoaded(j.result);
  else showResult(j.result);
}

/* ---- load playlist ---- */
async function load(){
  err('err'); spin('loadspin',true); $('loadbtn').disabled=true;
  try{ await post('/api/load',{source:$('source').value,refresh:$('refresh').checked}); }
  catch(e){ err('err',e.message); spin('loadspin',false); $('loadbtn').disabled=false; return; }
  startProgress();
}
function onLoaded(r){
  TRACKS=r.tracks;
  $('plpick').style.display='block';
  $('plstats').textContent=`${r.total} songs loaded (${r.with_data} ready to mix)`;
  PLSEED=null; $('plsel').classList.remove('show'); $('plsearch').value='';
  updateGo();
}

/* ---- any-song search (global) ---- */
function anyInput(){
  clearTimeout(T1);
  const q=$('anysearch').value.trim();
  if(q.length<2){$('anydd').style.display='none';return;}
  T1=setTimeout(()=>anySearch(q),420);
}
async function anySearch(q){
  err('err'); spin('anyspin',true);
  let j; try{ j=await post('/api/search',{q}); }
  catch(e){ spin('anyspin',false); err('err',e.message); return; }
  spin('anyspin',false);
  ANYRES=j.results||[];
  const dd=$('anydd');
  if(!ANYRES.length){dd.style.display='none';return;}
  dd.innerHTML=ANYRES.map((t,i)=>`<div class="opt" onclick="pickAny(${i})">
    <div><div class="n">${esc(t.name)}</div><div class="a">${esc(t.artist)}</div></div></div>`).join('');
  dd.style.display='block';
}
function pickAny(i){
  ANY=ANYRES[i]; $('anydd').style.display='none'; $('anysearch').value='';
  $('anyname').textContent=ANY.name; $('anyartist').textContent=ANY.artist;
  $('anysel').classList.add('show'); updateGo();
}
function clearAny(){ANY=null;$('anysel').classList.remove('show');updateGo();$('anysearch').focus();}

/* ---- playlist seed search (local) ---- */
function plInput(){
  const q=$('plsearch').value.toLowerCase().trim();
  const dd=$('pldd');
  if(q.length<2||!TRACKS.length){dd.style.display='none';return;}
  const m=TRACKS.filter(t=>(t.artist+' '+t.name).toLowerCase().includes(q))
    .sort((a,b)=>((b.bpm||b.camelot)?1:0)-((a.bpm||a.camelot)?1:0)).slice(0,12);
  if(!m.length){dd.style.display='none';return;}
  dd.innerHTML=m.map(t=>`<div class="opt" onclick="pickPl('${t.id}')">
    <div><div class="n">${esc(t.name)}</div><div class="a">${esc(t.artist)}</div></div>
    <div>${chips(t)}</div></div>`).join('');
  dd.style.display='block';
}
function pickPl(id){
  PLSEED=TRACKS.find(t=>t.id===id);
  $('pldd').style.display='none'; $('plsearch').value='';
  $('plname2').textContent=PLSEED.name; $('plartist').textContent=PLSEED.artist;
  $('plsel').classList.add('show'); updateGo();
}
function clearPl(){PLSEED=null;$('plsel').classList.remove('show');updateGo();$('plsearch').focus();}

/* ---- go ---- */
async function go(){
  err('err'); spin('gospin',true); $('gobtn').disabled=true;
  try{
    if(MODE==='any') await post('/api/discover',{seed:ANY,count:COUNT});
    else await post('/api/mix',{seed_id:PLSEED.id,count:COUNT});
  }catch(e){ err('err',e.message); spin('gospin',false); updateGo(); return; }
  startProgress();
}
async function sortAll(){
  err('err'); $('sortbtn').disabled=true;
  try{ await post('/api/mix',{ids:TRACKS.map(t=>t.id),sort_all:true}); }
  catch(e){ err('err',e.message); $('sortbtn').disabled=false; return; }
  startProgress();
}

function showResult(r){
  $('sortbtn').disabled=false;
  CHAIN=r.chain;
  $('avg').textContent=` - flow ${r.avg}/100`+(r.pool?` (from ${r.pool} similar songs)`:'');
  $('result').innerHTML=CHAIN.map((t,i)=>{
    let s='<span class="num">start</span>';
    if(t.score!==null){const c=t.score>=80?'s-great':t.score>=55?'s-ok':'s-bad';
      s=`<span class="score ${c}">${Math.round(t.score)}</span>`;}
    return `<tr><td class="num">${i+1}</td>
      <td><div class="tname">${esc(t.name)}</div><div class="tartist">${esc(t.artist)}</div></td>
      <td>${t.bpm?Math.round(t.bpm):'?'}</td><td>${t.camelot||'?'}</td><td>${s}</td></tr>`;
  }).join('');
  $('plnameinput').value=r.seedName?`AutoMix · ${r.seedName}`:'AutoMix';
  $('resultcard').style.display='block';
  $('rlink').innerHTML='';
  $('resultcard').scrollIntoView({behavior:'smooth'});
}

async function createPl(){
  err('createerr'); spin('createspin',true); $('createbtn').disabled=true;
  try{
    const j=await post('/api/create',{name:$('plnameinput').value,uris:CHAIN.map(t=>t.uri)});
    $('rlink').innerHTML=`Done! <a href="${j.url}" target="_blank">Open on Spotify &rarr;</a>`;
  }catch(e){ err('createerr',e.message); }
  spin('createspin',false); $('createbtn').disabled=false;
}

document.addEventListener('click',e=>{
  if(!e.target.closest('.searchwrap')){$('anydd').style.display='none';$('pldd').style.display='none';}
});
$('source').addEventListener('keydown',e=>{if(e.key==='Enter')load();});
</script>
</body>
</html>
"""


@app.post("/api/search")
def api_search():
    q = (request.get_json(force=True).get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    try:
        res = _sp().search(q=q, type="track", limit=10)
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            return jsonify({"error": "Spotify rate limit - try again shortly."}), 429
        if e.http_status in (401, 403):
            return jsonify({"error": "Spotify blocked search for this app."}), 403
        return jsonify({"error": f"Spotify error (HTTP {e.http_status})"}), 502
    items = (res.get("tracks") or {}).get("items") or []
    return jsonify({"results": [{
        "id": t["id"], "name": t["name"],
        "artist": ", ".join(a["name"] for a in t["artists"]),
        "uri": t["uri"],
    } for t in items if t and t.get("id")]})


if __name__ == "__main__":
    print("\n  AutoMix UI -> http://127.0.0.1:7777\n")
    app.run(host="127.0.0.1", port=7777, debug=False, threaded=True)
