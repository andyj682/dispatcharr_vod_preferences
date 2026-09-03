#!/usr/bin/env bash
# rank_dryrun.sh — show the plugin's DETERMINISTIC ranking/decision for a given
# episode from the current Dispatcharr DB state, WITHOUT changing log levels.
# Loads the installed plugin's patch.py and runs its real ranking logic, so you
# can see exactly which stream the plugin would pick (and why) for any title.
#
# Run this on the Dispatcharr host (wherever `docker` is). Edit the -e vars below,
# then paste the whole block.
#
#   Pick an episode by numeric episode ID:            -e EP_ID="1234"
#   ...OR by series name + season + episode:          -e SERIES="Example Show" -e SEASON="1" -e EPISODE="1"
#   (EP_ID wins if set. SERIES matches as a WHOLE WORD — a short title won't
#    match a longer word that merely contains it.)
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
  -e EP_ID="" \
  -e SERIES="Example Show" -e SEASON="1" -e EPISODE="1" \
  dispatcharr python manage.py shell << 'PY'
import os, re, importlib, importlib.util
try:
    vp = importlib.import_module('dispatcharr_vod_preferences.patch')
except Exception:
    pdir = os.environ.get('DISPATCHARR_PLUGINS_DIR') or os.environ.get('PLUGINS_DIR') or '/data/plugins'
    sp = importlib.util.spec_from_file_location('vp', os.path.join(pdir, 'dispatcharr_vod_preferences', 'patch.py'))
    vp = importlib.util.module_from_spec(sp); sp.loader.exec_module(vp)

from apps.vod.models import Episode

EP_ID = (os.environ.get('EP_ID') or '').strip()
if EP_ID:
    e = Episode.objects.select_related('series').get(id=int(EP_ID))
else:
    tok = (os.environ.get('SERIES') or '').strip().lower()
    season = int(os.environ.get('SEASON') or 0)
    episode = int(os.environ.get('EPISODE') or 0)
    matches = list(Episode.objects.filter(season_number=season, episode_number=episode,
                   series__name__icontains=tok).select_related('series'))
    def word_match(name):
        return re.search(r'(?<![a-z0-9])' + re.escape(tok) + r'(?![a-z0-9])', name.lower()) is not None
    e = next((x for x in matches if word_match(x.series.name)), None) or (matches[0] if matches else None)
    assert e is not None, f"No match for SERIES={tok!r} S{season:02d}E{episode:02d}; got {[m.series.name for m in matches]}"

s = e.series
cfg = vp._load_config(force=True)

def row(c):
    return (f"acct={c.m3u_account.name!r} prio={c.m3u_account.priority} id={c.id} "
            f"stream={c.stream_id} tier={vp.quality_tier(c)} "
            f"dv_nofb={vp._is_dv_no_fallback(c)} audio={vp._audio_label(c)}")

print("EPISODE", e.name, "| series", s.name)
print(f"SETTINGS: prefer_quality={cfg['prefer_quality']} prefer_audio={cfg['prefer_audio']} "
      f"avoid_dv_no_fallback={cfg['avoid_dv_no_fallback']} remember_ui_picks={cfg['remember_ui_picks']}")
picks = cfg['saved_picks']
sk = [k for k in (f"stmdb:{s.tmdb_id}", f"simdb:{s.imdb_id}", f"series:{s.uuid}") if k in picks]
print("SAVED PICK for this series:", {k: picks[k] for k in sk} or "none")

cands = list(e.m3u_relations.filter(m3u_account__is_active=True)
             .select_related('m3u_account').order_by('-m3u_account__priority', 'id'))
print("\nNATIVE ORDER (Dispatcharr account priority, then id):")
for i, c in enumerate(cands):
    print(f"  {i}: {row(c)}")

rel, ranked, reason = vp._apply_preferences(e, cands[0], cands, None, None)
print(f"\nPLUGIN DECISION (current settings): reason={reason}")
print(f"  -> {row(rel)}")
print("  failover order:", [(c.m3u_account.name, c.stream_id) for c in ranked])

print("\nDETERMINISTIC RANKING under prefer=4k (independent of session/capacity):")
for av in (False, True):
    r = vp._quality_ranked(cands, "4k", cfg['prefer_audio'], av)
    print(f"  avoid_dv={av} -> " + str([(c.m3u_account.name, c.stream_id, vp.quality_tier(c)) for c in r]))
PY
