#!/usr/bin/env bash
# rank_dryrun.sh — show the plugin's DETERMINISTIC ranking/decision for a given
# MOVIE or EPISODE from the current Dispatcharr DB state, WITHOUT changing log
# levels. Loads the installed plugin's patch.py and runs its real ranking logic,
# so you can see exactly which stream the plugin would pick (and why) for a title.
#
# Run this on the Dispatcharr host (wherever `docker` is). Edit the -e vars below,
# then paste the whole block.
#
#   KIND      "episode" (default) or "movie"
#   ID        numeric episode-id or movie-id (matching KIND); wins if set
#   TITLE     series name (episode) or movie title (movie); matched as a WHOLE WORD
#             (a short title won't match a longer word that merely contains it)
#   SEASON    season number   (episode only)
#   EPISODE   episode number  (episode only)
#
# Examples (edit the -e lines below to taste):
#   KIND=episode  TITLE="Example Show"  SEASON=1  EPISODE=1
#   KIND=episode  ID=1234
#   KIND=movie    TITLE="Example Movie"
#   KIND=movie    ID=5678
#
# Change the container name "dispatcharr" if yours differs (docker ps to check).
#
# How to read the output:
#   * NATIVE ORDER      = what Dispatcharr feeds the plugin (account priority, then id).
#   * PLUGIN DECISION   = the plugin's actual pick + reason under your live settings.
#   * DETERMINISTIC ...  = the plugin's pure ranking (no session/capacity effects).
# If real playback ever differs from PLUGIN DECISION, it's Dispatcharr-side
# (provider at capacity -> failover, or idle-session reuse), NOT the plugin.

docker exec -i \
  -e KIND="episode" \
  -e ID="" \
  -e TITLE="Example Show" \
  -e SEASON="1" -e EPISODE="1" \
  dispatcharr python manage.py shell << 'PY'
import os, re, importlib, importlib.util
try:
    vp = importlib.import_module('dispatcharr_vod_preferences.patch')
except Exception:
    pdir = os.environ.get('DISPATCHARR_PLUGINS_DIR') or os.environ.get('PLUGINS_DIR') or '/data/plugins'
    sp = importlib.util.spec_from_file_location('vp', os.path.join(pdir, 'dispatcharr_vod_preferences', 'patch.py'))
    vp = importlib.util.module_from_spec(sp); sp.loader.exec_module(vp)

from apps.vod.models import Movie, Episode

KIND = (os.environ.get('KIND') or 'episode').strip().lower()
ID = (os.environ.get('ID') or '').strip()
TOK = (os.environ.get('TITLE') or '').strip().lower()

def word_match(name):
    return re.search(r'(?<![a-z0-9])' + re.escape(TOK) + r'(?![a-z0-9])', name.lower()) is not None

if KIND == 'movie':
    if ID:
        obj = Movie.objects.get(id=int(ID))
    else:
        matches = list(Movie.objects.filter(name__icontains=TOK))
        obj = next((x for x in matches if word_match(x.name)), None) or (matches[0] if matches else None)
        assert obj is not None, f"No movie matching TITLE={TOK!r}; got {[x.name for x in matches][:10]}"
    label = f"MOVIE {obj.name} | tmdb {obj.tmdb_id} | imdb {obj.imdb_id} | uuid {obj.uuid}"
else:
    season = int(os.environ.get('SEASON') or 0)
    episode = int(os.environ.get('EPISODE') or 0)
    if ID:
        obj = Episode.objects.select_related('series').get(id=int(ID))
    else:
        matches = list(Episode.objects.filter(season_number=season, episode_number=episode,
                       series__name__icontains=TOK).select_related('series'))
        obj = next((x for x in matches if word_match(x.series.name)), None) or (matches[0] if matches else None)
        assert obj is not None, f"No episode matching TITLE={TOK!r} S{season:02d}E{episode:02d}; got {[m.series.name for m in matches]}"
    s = obj.series
    label = f"EPISODE {obj.name} | series {s.name}"

cfg = vp._load_config(force=True)

def row(c):
    return (f"acct={c.m3u_account.name!r} prio={c.m3u_account.priority} id={c.id} "
            f"stream={c.stream_id} tier={vp.quality_tier(c)} "
            f"dv_nofb={vp._is_dv_no_fallback(c)} audio={vp._audio_label(c)}")

print(label)
print(f"SETTINGS: prefer_quality={cfg['prefer_quality']} prefer_audio={cfg['prefer_audio']} "
      f"avoid_dv_no_fallback={cfg['avoid_dv_no_fallback']} remember_ui_picks={cfg['remember_ui_picks']}")

cands = list(obj.m3u_relations.filter(m3u_account__is_active=True)
             .select_related('m3u_account').order_by('-m3u_account__priority', 'id'))

picks = cfg['saved_picks']
if KIND == 'movie':
    keys = ([f"tmdb:{obj.tmdb_id}"] if obj.tmdb_id else []) + ([f"imdb:{obj.imdb_id}"] if obj.imdb_id else [])
    hits = {k: picks[k] for k in keys if k in picks}
    for c in cands:
        fk = f"as:{c.m3u_account_id}:{c.stream_id}"
        if fk in picks:
            hits[fk] = picks[fk]
else:
    keys = [f"stmdb:{s.tmdb_id}", f"simdb:{s.imdb_id}", f"series:{s.uuid}"]
    hits = {k: picks[k] for k in keys if k in picks}
print("SAVED PICK:", hits or "none")

print("\nNATIVE ORDER (Dispatcharr account priority, then id):")
for i, c in enumerate(cands):
    print(f"  {i}: {row(c)}")

if not cands:
    print("\n(no active provider relations for this title)")
else:
    rel, ranked, reason = vp._apply_preferences(obj, cands[0], cands, None, None)
    print(f"\nPLUGIN DECISION (current settings): reason={reason}")
    print(f"  -> {row(rel)}")
    print("  failover order:", [(c.m3u_account.name, c.stream_id) for c in ranked])

    print("\nDETERMINISTIC RANKING under prefer=4k (independent of session/capacity):")
    for av in (False, True):
        r = vp._quality_ranked(cands, "4k", cfg['prefer_audio'], av)
        print(f"  avoid_dv={av} -> " + str([(c.m3u_account.name, c.stream_id, vp.quality_tier(c)) for c in r]))
PY
