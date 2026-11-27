#!/usr/bin/env bash
# wanted_survey.sh -- READ-ONLY survey of episode stream info for the shows you
# actually watch. Reads ONLY the Dispatcharr database: no provider calls, no
# writes, no settings changes. Safe to run any time.
#
# "Shows you watch" = the VOD Episode Sweep plugin's watchlist (its own
# CoreSettings row). Set SCOPE=all to survey every episode relation instead.
#
# Two reports:
#   1. INFO COVERAGE   -- how many episode copies carry video size / audio /
#                         bitrate info, how many carry NONE, per provider, and
#                         how many episodes have a copy the ranking can't see
#                         (the size of a targeted episode-enrichment job).
#   2. BITRATE SURVEY  -- the video bitrate of every copy that has one,
#                         codec-adjusted to an H.264 equivalent, per resolution
#                         tier, against a range of candidate "starved" floors;
#                         plus how many episodes a floor would actually change.
#
# Run this on the Dispatcharr host (wherever `docker` is). Paste the whole block.
#   SCOPE   "wanted" (default) or "all"
#   WORST   how many lowest-bitrate copies / changed episodes to list (default 15)
#   FLOOR   override the "starved" floors used for the impact count, e.g.
#           FLOOR="4k=6,1080p=1.5,720p=0.8,480p=0.4" (Mbps, H.264-equivalent)
#
# Change the container name "dispatcharr" if yours differs (docker ps to check).
#
# How bitrate is read (mirrors what a ranking rule would use):
#   * video-only:  the video track's BPS tag (or its bit_rate) when present;
#   * otherwise:   the file's total bitrate minus the first audio track's bitrate.
#     Totals are stored as kbps by providers and as bps by ffprobe enrichment;
#     both are normalized. A file with many audio tracks overstates its video
#     bitrate on this path (only one audio track is ever recorded) -- the
#     "source" counts show how many copies relied on it.
#   * H.264 equivalent: HEVC/AV1 x1.5, VP9 x1.3, MPEG-2 x0.6, MPEG-4 x0.8.

docker exec -i \
  -e SCOPE="wanted" \
  -e WORST="15" \
  -e FLOOR="" \
  dispatcharr python manage.py shell << 'PY'
import os, json, importlib, importlib.util
from collections import Counter, defaultdict
try:
    vp = importlib.import_module('dispatcharr_vod_preferences.patch')
except Exception:
    pdir = os.environ.get('DISPATCHARR_PLUGINS_DIR') or os.environ.get('PLUGINS_DIR') or '/data/plugins'
    sp = importlib.util.spec_from_file_location('vp', os.path.join(pdir, 'dispatcharr_vod_preferences', 'patch.py'))
    vp = importlib.util.module_from_spec(sp); sp.loader.exec_module(vp)

from core.models import CoreSettings
from apps.vod.models import M3UEpisodeRelation

SCOPE = (os.environ.get('SCOPE') or 'wanted').strip().lower()
WORST = int(os.environ.get('WORST') or 15)
WATCHLIST_KEY = 'dispatcharr_vod_episode_sweep_watchlist'

TIERS = ('4k', '1080p', '720p', '480p')
FLOORS = {'4k': (4, 6, 8, 10, 12), '1080p': (1, 1.5, 2, 2.5, 3, 4),
          '720p': (0.5, 0.8, 1, 1.2, 1.5), '480p': (0.3, 0.5, 0.7, 1)}
DEFAULT_FLOOR = {'4k': 8, '1080p': 2.5, '720p': 1.2, '480p': 0.6}
for part in (os.environ.get('FLOOR') or '').split(','):
    k, _, v = part.partition('=')
    if k.strip().lower() in DEFAULT_FLOOR and v.strip():
        DEFAULT_FLOOR[k.strip().lower()] = float(v)
CODEC_FACTOR = {'hevc': 1.5, 'h265': 1.5, 'av1': 1.5, 'vp9': 1.3,
                'mpeg2video': 0.6, 'mpeg4': 0.8}


def num(v):
    try:
        f = float(str(v).strip().split()[0])
        return f if f > 0 else None
    except Exception:
        return None


def to_bps(v):
    # Providers store total bitrate in kbps (e.g. 4842); ffprobe in bps.
    n = num(v)
    if n is None:
        return None
    return n * 1000 if n < 200000 else n


def video_dict(rel):
    for v in vp._iter_video_dicts(rel):
        if not vp._is_cover_image(v):
            return v
    return None


def total_bps(rel):
    cp = vp._custom_props(rel)
    info = cp.get('info') if isinstance(cp.get('info'), dict) else {}
    for d in (cp.get('detailed_info'), info, info.get('info'), cp.get('basic_data'), cp.get('ffprobe_info')):
        if isinstance(d, dict) and d.get('bitrate') not in (None, ''):
            b = to_bps(d.get('bitrate'))
            if b:
                return b
    return None


def audio_bps(a):
    if not isinstance(a, dict):
        return None
    tags = a.get('tags') if isinstance(a.get('tags'), dict) else {}
    return num(a.get('bit_rate')) or num(tags.get('BPS')) or num(tags.get('BPS-eng'))


def video_bps(rel, v, a):
    """-> (bps, source) or (None, None)."""
    if isinstance(v, dict):
        tags = v.get('tags') if isinstance(v.get('tags'), dict) else {}
        for k in ('BPS', 'BPS-eng'):
            b = num(tags.get(k))
            if b and b > 50000:
                return b, 'video tag'
        b = num(v.get('bit_rate'))
        if b and b > 50000:
            return b, 'video stream'
    t = total_bps(rel)
    if t:
        ab = audio_bps(a)
        if ab and t - ab > 50000:
            return t - ab, 'total-audio'
        return t, 'total only'
    return None, None


def analyze(rel):
    v = video_dict(rel)
    a = vp._audio_dict(rel)
    codec = str((v or {}).get('codec_name') or '').lower()
    dims_tier = vp._tier_from_dims(v.get('width'), v.get('height')) if v else None
    vb, src = video_bps(rel, v, a)
    eq = vb * CODEC_FACTOR.get(codec, 1.0) / 1e6 if vb else None
    has_audio = bool(isinstance(a, dict) and a.get('codec_name'))
    return {
        'rel': rel, 'codec': codec or '?', 'dims_tier': dims_tier,
        'plugin_tier': vp.quality_tier(rel), 'eq': eq, 'src': src,
        'has_dims': dims_tier is not None, 'has_audio': has_audio,
        'has_br': vb is not None,
        'none': not (dims_tier or has_audio or vb or codec),
    }


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def label(rel):
    e = rel.episode
    return (f"{e.series.name} S{e.season_number or 0:02d}E{e.episode_number or 0:02d} "
            f"| {rel.m3u_account.name} id={rel.id}")


def main():
    if SCOPE == 'wanted':
        row = CoreSettings.objects.filter(key=WATCHLIST_KEY).values('value').first()
        val = (row or {}).get('value') or {}
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                val = {}
        series = val.get('series') if isinstance(val, dict) else None
        pks = [int(k) for k in (series or {}) if str(k).isdigit()]
        if not pks:
            print("Watchlist is empty or missing -- is VOD Episode Sweep installed? Try SCOPE=all.")
            return
        qs = M3UEpisodeRelation.objects.filter(episode__series_id__in=pks)
        print(f"SCOPE: wanted list ({len(pks)} series on the Episode Sweep watchlist)")
    else:
        qs = M3UEpisodeRelation.objects.all()
        print("SCOPE: all episode relations")

    cfg = vp._load_config(force=True)
    print(f"LIVE SETTINGS: prefer_quality={cfg['prefer_quality']} prefer_audio={cfg['prefer_audio']} "
          f"avoid_dv_no_fallback={cfg['avoid_dv_no_fallback']}")
    print("FLOORS (Mbps, H.264-eq): " + ", ".join(f"{t}={DEFAULT_FLOOR[t]}" for t in TIERS))
    print("(read-only: database only, no provider calls)\n")

    qs = (qs.filter(m3u_account__is_active=True)
            .select_related('m3u_account', 'episode', 'episode__series')
            .order_by('episode_id', '-m3u_account__priority', 'id'))

    acct = defaultdict(Counter)
    src_count = Counter()
    eq_by_tier = defaultdict(list)
    eq_by_acct_tier = defaultdict(list)
    codec_by_tier = defaultdict(Counter)
    lows = []                         # (ratio to default floor, eq, tier, codec, label)
    ep_rows = []                      # (series_id, season, n_copies, n_none, n_blind)
    changed = []                      # episodes a default floor would change
    n_rel = 0

    def finish_episode(rows):
        if not rows:
            return
        e = rows[0]['rel'].episode
        n_none = sum(r['none'] for r in rows)
        n_blind = sum(1 for r in rows if not (r['has_dims'] and r['has_audio']))
        ep_rows.append((e.series_id, e.season_number or 0, len(rows), n_none, n_blind))
        if len(rows) < 2 or not cfg['prefer_quality'] or cfg['prefer_quality'] == 'off':
            return
        cands = [r['rel'] for r in rows]
        by_id = {r['rel'].id: r for r in rows}
        ranked = vp._quality_ranked(cands, cfg['prefer_quality'], cfg['prefer_audio'],
                                    cfg['avoid_dv_no_fallback'])
        top = by_id[ranked[0].id]
        floor = DEFAULT_FLOOR.get(top['plugin_tier'])
        if top['eq'] is None or floor is None or top['eq'] >= floor:
            return
        for c in ranked[1:]:
            r = by_id[c.id]
            if r['plugin_tier'] != top['plugin_tier']:
                continue
            if cfg['avoid_dv_no_fallback'] and vp._is_dv_no_fallback(c):
                continue
            if r['eq'] is not None and r['eq'] >= floor:
                changed.append((top, r))
                return

    cur_ep, rows = None, []
    for rel in qs.iterator(chunk_size=2000):
        n_rel += 1
        r = analyze(rel)
        name = rel.m3u_account.name
        acct[name]['copies'] += 1
        if r['has_dims'] and r['has_audio']:
            acct[name]['complete'] += 1
        elif r['none']:
            acct[name]['none'] += 1
        else:
            acct[name]['partial'] += 1
        if r['has_br']:
            acct[name]['bitrate'] += 1
        if r['eq'] is not None and r['dims_tier']:
            t = r['dims_tier']
            src_count[r['src']] += 1
            eq_by_tier[t].append(r['eq'])
            eq_by_acct_tier[(name, t)].append(r['eq'])
            codec_by_tier[t][r['codec']] += 1
            lows.append((r['eq'] / DEFAULT_FLOOR[t], r['eq'], t, r['codec'], label(rel)))
        if rel.episode_id != cur_ep:
            finish_episode(rows)
            cur_ep, rows = rel.episode_id, []
        rows.append(r)
    finish_episode(rows)

    if not n_rel:
        print("No active episode relations in scope.")
        return

    # ---------------- 1. INFO COVERAGE ----------------
    print("=" * 78)
    print("1. INFO COVERAGE  (complete = video size AND audio; none = no video/audio/bitrate at all)")
    print("=" * 78)
    print(f"{'provider':<20}{'copies':>9}{'complete':>10}{'partial':>9}{'none':>8}{'bitrate':>9}")
    tot = Counter()
    for name in sorted(acct, key=lambda k: -acct[k]['copies']):
        c = acct[name]
        tot.update(c)
        print(f"{name[:19]:<20}{c['copies']:>9}{c['complete']:>10}{c['partial']:>9}{c['none']:>8}{c['bitrate']:>9}")
    print(f"{'TOTAL':<20}{tot['copies']:>9}{tot['complete']:>10}{tot['partial']:>9}{tot['none']:>8}{tot['bitrate']:>9}")

    latest = {}
    for sid, season, *_ in ep_rows:
        latest[sid] = max(latest.get(sid, 0), season)
    def ep_summary(rows_):
        eps = len(rows_)
        multi = [x for x in rows_ if x[2] >= 2]
        blind = [x for x in multi if x[4] >= 1]
        all_none = [x for x in rows_ if x[3] == x[2]]
        need = sum(x[4] for x in blind)
        return eps, len(multi), len(blind), len(all_none), need
    print()
    for title, sel in (("all seasons", ep_rows),
                       ("latest season of each show", [x for x in ep_rows if x[1] == latest.get(x[0])])):
        eps, multi, blind, all_none, need = ep_summary(sel)
        print(f"Episodes ({title}): {eps}")
        print(f"  with 2+ copies:                          {multi}")
        print(f"  ...where 1+ copy lacks size or audio:    {blind}   <- ranking partly blind")
        print(f"  with NO info on any copy:                {all_none}")
        print(f"  copies to probe to fix the blind ones:   {need}"
              f"   (~{need * 5 / 3600:.1f} h at one probe per 5 s, one provider at a time)")

    # ---------------- 2. BITRATE SURVEY ----------------
    print()
    print("=" * 78)
    print("2. BITRATE SURVEY  (video Mbps, H.264-equivalent; tier from measured video size only)")
    print("=" * 78)
    print("bitrate source:", ", ".join(f"{k}={v}" for k, v in src_count.most_common()) or "none")
    for t in TIERS:
        vals = eq_by_tier.get(t)
        if not vals:
            continue
        print(f"\n[{t}] {len(vals)} copies   codecs: "
              + ", ".join(f"{k}={v}" for k, v in codec_by_tier[t].most_common()))
        print("  percentiles  " + "  ".join(f"p{p}={pct(vals, p):.2f}" for p in (5, 10, 25, 50, 75, 90)))
        print("  below floor  " + "  ".join(
            f"<{f}:{sum(1 for x in vals if x < f)}" + ("*" if f == DEFAULT_FLOOR[t] else "")
            for f in FLOORS[t]) + "   (* = proposed default)")
        for (name, tt), xs in sorted(eq_by_acct_tier.items(), key=lambda kv: kv[0]):
            if tt == t:
                below = sum(1 for x in xs if x < DEFAULT_FLOOR[t])
                print(f"    {name[:19]:<20} n={len(xs):<6} median={pct(xs, 50):.2f}  "
                      f"below default floor={below}")

    print(f"\nLOWEST {WORST} copies relative to the proposed floor:")
    for ratio, eq, t, codec, lab in sorted(lows)[:WORST]:
        print(f"  {eq:6.2f} Mbps-eq  {t:<6}{codec:<6} {lab}")

    print(f"\nIMPACT under your live settings with the proposed floors: {len(changed)} episode(s) "
          "where today's top pick is starved and a non-starved copy of the same tier exists.")
    for top, alt in sorted(changed, key=lambda x: x[0]['eq'])[:WORST]:
        print(f"  [{top['plugin_tier']}] {label(top['rel'])}  {top['eq']:.2f} {top['codec']}  ->  "
              f"{alt['rel'].m3u_account.name} id={alt['rel'].id}  {alt['eq']:.2f} {alt['codec']}")
    print("\n(Saved picks are ignored here; an episode with a saved pick would keep it.)")


main()
PY
