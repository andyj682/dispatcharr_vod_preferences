"""
VOD stream-preference monkeypatch for current Dispatcharr.

Problem
-------
When Dispatcharr proxies a VOD title (movie / episode) to a client -- including
the Xtream-Codes path clients play through -- it picks *which* provider stream to
serve purely by account priority. `apps/proxy/vod_proxy/views._get_content_and_relation`
builds the active `M3UMovieRelation` / `M3UEpisodeRelation` rows ordered only by
`-m3u_account__priority, id` and returns `candidates[0]` (unless the request
carries an explicit `stream_id` / `m3u_account_id`). There is no way to say
"for this title, prefer the 4K stream" or "use the stream I picked once in the
UI". Adding real per-title ordering is a core/frontend change; this plugin
delivers the practical subset with a single, reload-safe monkeypatch.

Fix (one hook, a small selection ladder)
-----------------------------------------
We wrap `views._get_content_and_relation`. It is reached on BOTH the UI
`/proxy/vod/` path (which passes the picked `stream_id`) and the Xtream-Codes
play path (native `stream_vod` calls it by module-global name), so one hook covers
both *capturing* a UI pick and *applying* preferences to a client's playback.

The wrapper calls the original, then re-decides `(relation, candidates)` with a
most-specific-first ladder:

  1. Explicit request pick -- the request carried `stream_id` and the original
     honoured it. If "remember my UI pick" is on, persist it as this title's
     default. Return the original result unchanged.
  2. Saved UI pick -- a remembered (account, stream) for this title. If that
     stream is still among the candidates, make it primary and move it to the
     front of the failover list. If the provider dropped it, fall through.
  3. Quality rule -- if "prefer quality" is set, stable-sort candidates by a
     quality rank derived from the same per-stream signal the UI shows; the best
     becomes primary and failover follows quality order. Streams with no quality
     signal keep their native account-priority order (stable tiebreak). When
     "prefer audio" is also on, audio rank (surround-first, then lossless >
     Dolby > AAC > other) is a SECONDARY key that breaks ties among streams of
     the same video tier. When "avoid DV without fallback" is on, a Dolby-Vision
     stream with no HDR10/SDR/HLG base layer is demoted below every compatible
     stream (the TOP key) so a playable copy wins; it's only served if nothing
     else is available. When "avoid low-bitrate copies" is on, a copy whose
     video bitrate is below its tier's minimum ranks after the healthy copies
     of the SAME tier (before audio), and a clearly higher bitrate is the last
     tiebreak. Full key: (compatible, tier, not starved, audio, bitrate band).
  4. Native -- account priority, untouched.

Composition: this plugin only reads/re-orders the relation list. It never
touches `stream_vod`, `_get_m3u_profile`, or reservation, so it composes cleanly
with `dispatcharr_vod_concurrency_fix` (disjoint functions).

Persistence
-----------
Saved picks live in this plugin's own `PluginConfig.settings` JSON (DB-backed ->
durable across restarts and across all uWSGI workers; Dispatcharr's Redis is
ephemeral). They are stored under the `saved_picks` key as
`{ title_key: {"m3u_account_id": <int>, "stream_id": "<str>"} }` and merged with
a row lock so a burst of identical picks (or a concurrent config save) can't
corrupt the map. A write only happens on an explicit UI pick that differs from
what is already stored, so the read-heavy proxy path stays write-free.

Title key (stable across the UUID-regenerating refresh of #961/#973):
`tmdb:<id>` -> `imdb:<id>` -> for episodes `ep:<series_uuid>:SxxExx` -> else the
picked stream itself as `as:<account_id>:<stream_id>`. The last form is applied
by scanning the candidates for that exact stream, so it behaves as a per-title
default whenever the picked stream is still offered.

Safety / runtime
-----------------
Import-safe: if the current Dispatcharr internals don't look the way we expect,
we don't patch and native behaviour is untouched. Per-request: any error inside
the wrapper falls back to the native `(content_obj, relation, candidates)`.
Config + saved picks are read through a short in-process TTL cache so a client's
"open-file burst" of a few requests costs at most one small DB read, and the
cache is invalidated immediately on our own writes. Under uWSGI's 4 lazy-apps
workers, enable + restart so every worker imports and patches. Each process
records its install state in a small Redis hash at install/uninstall, and the
Status action reports the stream-serving (uWSGI) workers from it -- since
Dispatcharr 0.34 the action itself runs in a Celery worker that never serves
streams, so "active in THIS process" stopped being the useful answer.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import types

logger = logging.getLogger("plugins.dispatcharr_vod_preferences")

# Dispatcharr's LOGGING config gives apps/celery/core/root their own handler+level
# but does NOT name "plugins.*", so this logger has no level of its own and
# inherits root's. That's fine in uWSGI (root=INFO), but a Celery prefork child
# has billiard reconfigure root to WARNING, which would silently discard our INFO
# lines there while core's apps.* lines survive. Adopt the "apps" logger's
# effective level (so DISPATCHARR_LOG_LEVEL still applies) so our records live
# wherever core's do. Guard on NOTSET so anything that already set a level (e.g.
# a test silencing this logger) keeps control. Preferences hooks the request path
# (never a prefork child today), so this is a cheap guarantee, not a live fix.
if logger.level == logging.NOTSET:
    logger.setLevel(logging.getLogger("apps").getEffectiveLevel() or logging.INFO)

# --------------------------------------------------------------------------- #
# Constants / tunables
# --------------------------------------------------------------------------- #

# Must match the installed plugin folder name (Dispatcharr derives the
# PluginConfig key as folder_name.replace(" ", "_").lower()).
PLUGIN_KEY = "dispatcharr_vod_preferences"

# Saved picks live in their OWN CoreSettings row, NOT in PluginConfig.settings.
# Reason: the Plugins UI re-saves the whole PluginConfig.settings snapshot it
# loaded with the card before running ANY action (and on every settings save),
# which would clobber picks the server wrote out-of-band. A separate CoreSettings
# row is untouched by that round-trip.
PICKS_CORE_KEY = "dispatcharr_vod_preferences_picks"
PICKS_CORE_NAME = "Dispatcharr VOD Preferences - saved picks"

# Legacy location (pre-0.1.1): picks used to live under this key inside
# PluginConfig.settings. Read as a one-time migration source until the
# CoreSettings row exists.
LEGACY_SAVED_PICKS_KEY = "saved_picks"

# How long a read of (config + saved picks) is cached in-process. Bounds DB
# reads on the proxy path to ~one per this window (covers a client burst) while
# staying fresh enough that a config change or a just-saved pick is honoured
# within a few seconds. Invalidated immediately on our own writes.
CONFIG_TTL_SECONDS = 5.0

# Defensive cap so a runaway never grows the settings JSON without bound. Far
# above any realistic number of manually-picked titles.
MAX_SAVED_PICKS = 10000

DEFAULT_PREFER_QUALITY = "off"
DEFAULT_REMEMBER_UI_PICKS = True
DEFAULT_PREFER_AUDIO = False
DEFAULT_AVOID_DV_NO_FALLBACK = False
DEFAULT_AVOID_LOW_BITRATE = False

# Minimum video bitrate per tier, in Mbps of H.264-equivalent video (see
# _CODEC_EFFICIENCY). A copy below its tier's minimum is "starved". Defaults were
# set from a survey of a large real-world library: each sits near the 5th-10th
# percentile of its tier, so only clearly broken encodes are caught. 0 (or any
# non-positive value) turns the minimum off for that tier.
DEFAULT_MIN_BITRATE = {"4k": 6.0, "1080p": 1.5, "720p": 0.8, "480p": 0.4}
MIN_BITRATE_SETTING = {tier: "min_bitrate_" + tier for tier in DEFAULT_MIN_BITRATE}

# Newer codecs reach the same visual quality at a lower bitrate. Multiplying a
# copy's measured bitrate by its codec's factor gives an "H.264-equivalent"
# figure, so one minimum per tier works across codecs. Unlisted codecs = 1.0.
_CODEC_EFFICIENCY = {
    "hevc": 1.5, "h265": 1.5, "av1": 1.5, "vp9": 1.3,
    "mpeg2video": 0.6, "mpeg4": 0.8,
}

# Providers store a file's total bitrate in kbps (e.g. 4842); ffprobe output
# (incl. vod_merge's enrichment) stores bps (e.g. "4841229"). No real video file
# is below 200 kbps or above 200,000 kbps, so the magnitude says which it is.
_KBPS_CUTOFF = 200000
# A per-track video bitrate below 50 kbps is not a real measurement.
_MIN_VIDEO_BPS = 50000
# Final tiebreak buckets: bitrates within the same ~1.5x band tie, so two copies
# of the same release (or near-identical encodes) keep their native order.
_BITRATE_BUCKET_RATIO = 1.5

# Dispatcharr 0.33+ lets a user tag a VOD category with a quality. Values are
# validated by core (apps/vod/utils.QUALITY_ORDER).
_CATEGORY_QUALITY_TIERS = {
    "4k": "4k", "1080p": "1080p", "720p": "720p", "480p": "480p", "sd": "480p",
}
# How long "does ANY category carry a quality tag?" is cached. While the answer is
# no (the common case), the category rung costs nothing per request.
CATEGORY_TAGS_TTL_SECONDS = 300.0
# Attribute used to hand a pre-fetched category tier to quality_tier() without
# changing its signature (set per selection, on the relation objects themselves).
_CAT_TIER_ATTR = "_vodpref_category_tier"

# Per-worker install registry (Redis hash), read by the Status action. Since
# Dispatcharr 0.34 actions run in a Celery worker that never serves streams, so
# "is the patch active HERE?" no longer answers the real question.
WORKERS_REDIS_KEY = "dispatcharr_vod_preferences:workers"
WORKER_RECORD_MAX_AGE = 14 * 86400   # prune records older than this on write

# Recent plays (Redis list, newest first): the last few DISTINCT titles the proxy
# served, so the user can point an avoid rule at "the copy that just played" --
# from a player they can't see which provider or stream that was. Kept short on
# purpose: the list is shown in a small action-result popup.
RECENT_REDIS_KEY = "dispatcharr_vod_preferences:recent"
RECENT_MAX = 5
# A repeat of the newest entry (same title AND copy) inside this window is the
# same play (players re-request; Dispatcharr 0.34 calls the hook twice per first
# request) and isn't re-recorded.
RECENT_REPEAT_SECONDS = 600
RECENT_LABEL_CHARS = 26   # title truncation in the popup

# Played shows (a PUBLIC, durable record other plugins may read -- e.g. VOD Merge
# & Enrich narrowing episode enrichment to shows actually being watched). One
# CoreSettings row, value:
#   {"version": 1, "series": {"<Series.id>": {"ts": <unix>, "season": n,
#     "episode": n, "episode_id": <Episode.id>, "max_season": n, "max_episode": n}}}
# season/episode = the latest episode played; max_* = the furthest one reached.
# Written once per NEW episode play (repeats of the same play are skipped), and
# entries older than PLAYED_RETENTION_DAYS are pruned on write. A DB row rather
# than Redis because Dispatcharr's Redis isn't persisted across container restarts.
PLAYED_CORE_KEY = "dispatcharr_vod_preferences_played"
PLAYED_CORE_NAME = "Dispatcharr VOD Preferences - played shows"
PLAYED_FORMAT_VERSION = 1
PLAYED_RETENTION_DAYS = 60

# Avoid rules live in their own CoreSettings row (same reason as the picks row:
# the Plugins UI round-trips PluginConfig.settings). Value = {"rules": [...]}.
AVOID_CORE_KEY = "dispatcharr_vod_preferences_avoid"
AVOID_CORE_NAME = "Dispatcharr VOD Preferences - avoid rules"
MAX_AVOID_RULES = 1000
AVOID_LIST_SHOWN = 8      # newest rules shown by 'List avoid rules'

# "Missed 4K" notification.
DEFAULT_NOTIFY_MISSED_4K = False
NOTIFY_DEDUPE_PREFIX = "dispatcharr_vod_preferences:notified:"
NOTIFY_DEDUPE_SECONDS = 86400   # at most one alert per title per day
NOTIFY_TIMEOUT_SECONDS = 5.0
PROWL_URL = "https://api.prowlapp.com/publicapi/add"
NOTIFY_APP_NAME = "Dispatcharr"

# Quality-token patterns, checked most-specific-first so "UHD"/"FHD" resolve
# before the plain "HD" of the 720p tier. Each token must be bounded by a
# non-alphanumeric (or string edge) so free-text names don't misfire -- e.g.
# "Wednesday" must NOT read as SD, "24K Gold" must NOT read as 4K, "The Hidden"
# must NOT read as HD. (?i) + [0-9a-z] boundary classes cover upper/lower case.
_TOKEN_TIERS = (
    ("4k", r"(?:4k|2160p?|uhd)"),
    ("1080p", r"(?:1080p?|fhd)"),
    ("720p", r"(?:720p?|hd)"),
    ("480p", r"(?:480p?|sd)"),
)
_TOKEN_RE = tuple(
    (tier, re.compile(r"(?<![0-9a-z])" + pat + r"(?![0-9a-z])", re.IGNORECASE))
    for tier, pat in _TOKEN_TIERS
)
# A WxH resolution string embedded in text (e.g. "1920x1080", "3840 x 2160").
_WH_RE = re.compile(r"(?<!\d)(\d{3,4})\s*[xX]\s*(\d{3,4})(?!\d)")

# prefer_quality value -> ordered tier priority (best first). Anything not "off"
# and not explicitly mapped is treated as 4K-first.
#
# The ordering rule for every entry is: [the target tier] + [all LOWER tiers,
# quality-descending] + [all HIGHER tiers, quality-ascending]. So a target's own
# tier wins, then we step DOWN before stepping UP, and the biggest (4K) file is
# always last when the target isn't itself 4K -- this keeps a bandwidth-minded
# "Prefer 1080p"/"Prefer 720p" from silently pulling a huge 4K stream.
_QUALITY_PRIORITY = {
    "4k": ("4k", "1080p", "720p", "480p"),
    "1080p": ("1080p", "720p", "480p", "4k"),
    "720p": ("720p", "480p", "1080p", "4k"),
    "480p": ("480p", "720p", "1080p", "4k"),
}

# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #

_ACTIVE = False

# Captured at install time; also tagged on the patched callable so a plugin
# reload can't mistake an already-patched function for the "original".
_orig_get_content_and_relation = None

_PATCH_TAG = "_vodpref_patched"

_pid_logged = set()

# In-process config cache (see CONFIG_TTL_SECONDS).
_cfg_lock = threading.Lock()
_cfg_cache = None
_cfg_cache_ts = 0.0

# Cached answer to "does any category carry a quality tag?" -> (bool, ts).
_cat_tags_lock = threading.Lock()
_cat_tags_cache = None

# Last play seen by THIS process, for repeat detection when Redis is down.
_last_play_local = {}

# Fallback notification dedupe when Redis is unavailable: {title_key: ts}.
_notified_local = {}

# Replaceable for tests: the function that actually performs a notification
# send in the background (see _notify_async).
_notify_sender = None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _log_pid_once(where: str) -> None:
    key = f"{where}:{os.getpid()}"
    if key not in _pid_logged:
        _pid_logged.add(key)
        logger.info("[VOD-PREF] active in worker pid=%s at %s", os.getpid(), where)


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return default


def _nonempty(value):
    """Return a trimmed string if truthy, else None (handles '' and whitespace)."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _custom_props(relation) -> dict:
    """relation.custom_properties as a dict (tolerating a JSON-string value)."""
    props = getattr(relation, "custom_properties", None)
    if isinstance(props, dict):
        return props
    if isinstance(props, str):
        try:
            parsed = json.loads(props)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


# --------------------------------------------------------------------------- #
# Quality ranking
# --------------------------------------------------------------------------- #

# Codecs that indicate an attached cover image, not a real video track.
_IMAGE_CODECS = {"png", "mjpeg", "mjpg", "bmp", "gif", "jpeg", "jpg", "webp"}


def _is_cover_image(v):
    """True if a ffprobe-style 'video' dict is actually an attached cover image
    (poster/thumbnail), whose pixel size must NOT be read as the stream's
    resolution -- a 4K episode often carries a 1920x1080 PNG poster that would
    otherwise mis-rank it as 1080p."""
    if not isinstance(v, dict):
        return False
    disp = v.get("disposition")
    if isinstance(disp, dict) and disp.get("attached_pic"):
        return True
    if str(v.get("codec_name") or "").lower() in _IMAGE_CODECS:
        return True
    tags = v.get("tags")
    if isinstance(tags, dict) and str(tags.get("mimetype") or "").lower().startswith("image/"):
        return True
    return False


# Cropped / mod-adjusted frames land a few percent under a nominal standard
# WIDTH: a 2.39:1 "cinemascope" master cut from a 1920-wide source is commonly
# 1918, 1916 or 1912 wide (mod-2/mod-16 encoder constraints, side crop), yet is
# unmistakably a 1080p stream. A small tolerance on the width thresholds pulls
# such frames up to the right tier. It is safe because adjacent standard widths
# are ~30-50% apart (1280 vs 1920 vs 3840), far wider than the tolerance, so no
# genuine lower-tier stream can cross a boundary. The tolerance only ever LOWERS
# a width boundary, so it can only PROMOTE a borderline stream to a higher tier,
# never demote one. Heights are the exact standard values and stay strict.
_DIM_TOLERANCE = 0.05


def _tier_from_dims(width, height):
    """Map video pixel dimensions to a tier (mirrors Dispatcharr's thresholds)."""
    try:
        w = int(width or 0)
        h = int(height or 0)
    except (TypeError, ValueError):
        return None
    big = max(w, h)  # robust to width/height ordering

    def wide(threshold):
        # >= the threshold, allowing a small crop/mod tolerance below it.
        return big >= threshold * (1 - _DIM_TOLERANCE)

    if wide(3840) or h >= 2160:
        return "4k"
    if wide(1920) or h >= 1080:
        return "1080p"
    if wide(1280) or h >= 720:
        return "720p"
    if wide(854) or h >= 480:
        return "480p"
    return None


def _tier_from_text(value):
    """Infer a tier from a free-text label / name (word-boundary matched)."""
    s = _nonempty(value)
    if not s:
        return None
    wh = _WH_RE.search(s)
    if wh:
        tier = _tier_from_dims(wh.group(1), wh.group(2))
        if tier:
            return tier
    for tier, rx in _TOKEN_RE:
        if rx.search(s):
            return tier
    return None


def quality_tier(relation):
    """Return the quality tier of a relation ('4k'/'1080p'/'720p'/'480p') or None.

    Reads a waterfall of per-stream signals. ACTUAL video pixel dimensions are
    the top signal wherever they exist -- they're ground truth and outrank any
    text label, so a stream mislabeled '4K' whose real track is 1920x1080 ranks
    as 1080p:

      1. real video pixel dimensions from a nested detail dict (movies:
         'detailed_info'; episodes: 'info'.'info'). Attached cover images
         (PNG/JPEG posters) are skipped so a 4K episode's 1920x1080 poster isn't
         misread as its resolution. Often absent until an advanced refresh runs.
      2. custom_properties 'quality' / 'resolution' (explicit provider label)
      3. the provider STREAM name (movies: 'basic_data'.'name'; episodes:
         'info'.'title' / 'info'.'name') -- the reliable per-stream signal when
         the provider puts '4K'/'2160p'/etc. in the title and dims are absent
      4. the CATEGORY quality tag (Dispatcharr 0.33+; set per category in the
         VOD category filter UI). User-asserted for a whole category, so it
         ranks below anything that describes this one stream. Read from a value
         pre-fetched per selection (_annotate_category_tiers); absent -> skipped.
      5. the provider/ACCOUNT name (relation.m3u_account.name) -- catches the
         common "separate 4K provider" setup where quality is encoded at the
         account level (e.g. an account named "... 4K"); this is the only 4K
         signal for episodes whose stream titles don't carry it

    Returns None when nothing matches, so unknown-quality streams keep their
    native account-priority order (stable tiebreak) rather than being reshuffled.
    """
    cp = _custom_props(relation)
    detailed_info = cp.get("detailed_info") if isinstance(cp.get("detailed_info"), dict) else None
    info = cp.get("info") if isinstance(cp.get("info"), dict) else None
    basic_data = cp.get("basic_data") if isinstance(cp.get("basic_data"), dict) else None

    # 1. Real video pixel dimensions (ground truth), skipping cover images.
    detail_dicts = [d for d in (detailed_info, info, basic_data) if d]
    if info is not None and isinstance(info.get("info"), dict):
        detail_dicts.append(info["info"])
    for d in detail_dicts:
        v = d.get("video")
        if isinstance(v, dict) and not _is_cover_image(v):
            tier = _tier_from_dims(v.get("width"), v.get("height"))
            if tier:
                return tier
        tier = _tier_from_dims(d.get("width"), d.get("height"))
        if tier:
            return tier

    # 2. Explicit per-stream quality/resolution label.
    for key in ("quality", "resolution"):
        tier = _tier_from_text(cp.get(key))
        if tier:
            return tier

    # 3. Provider stream name.
    name_fields = []
    if basic_data is not None:
        name_fields.append(basic_data.get("name"))
    if info is not None:
        name_fields += [info.get("title"), info.get("name")]
    if detailed_info is not None:
        name_fields.append(detailed_info.get("name"))
    for nm in name_fields:
        tier = _tier_from_text(nm)
        if tier:
            return tier

    # 4. Category quality tag (pre-fetched; None when untagged or unavailable).
    tier = getattr(relation, _CAT_TIER_ATTR, None)
    if tier in _QUALITY_PRIORITY["4k"]:
        return tier

    # 5. Provider / account name.
    account = getattr(relation, "m3u_account", None)
    if account is not None:
        tier = _tier_from_text(getattr(account, "name", None))
        if tier:
            return tier

    return None


def quality_rank(relation, priority):
    """Score a relation by its tier's position in *priority* (higher = better).

    Unknown quality -> 0, which sorts last but, because the sort is stable,
    keeps the native account-priority order among unknowns.
    """
    tier = quality_tier(relation)
    if tier is None:
        return 0
    try:
        return len(priority) - priority.index(tier)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# Audio ranking (optional within-tier tiebreak)
# --------------------------------------------------------------------------- #

# Dolby (AC-3 family) codec names as reported by ffprobe / provider probes.
_AUDIO_DOLBY = {"ac3", "eac3", "e-ac-3", "ac-3"}

# Lossless / high-bitrate formats, ranked ABOVE lossy Dolby/AAC. ffprobe reports
# the whole DTS family (DTS, DTS-HD MA/HRA, DTS:X) as codec_name "dts", and Dolby
# TrueHD as "truehd" (MLP is its lossless core; "dca" is an older name for DTS).
_AUDIO_LOSSLESS = {"truehd", "mlp", "dts", "dca", "dts-hd"}

# (channel_group, codec_group) -> rank, higher is better. Surround-first: ANY
# multichannel beats ANY stereo; within a channel group lossless (TrueHD/DTS) >
# Dolby (AC3/EAC3) > AAC > other. Anything not classifiable (mono / unknown
# channel count, or no audio info) is rank 0 -> keeps native stable order, so
# audio never reshuffles a stream it has no opinion about.
_AUDIO_RANKS = {
    ("surround", "lossless"): 8,
    ("surround", "dolby"): 7,
    ("surround", "aac"): 6,
    ("surround", "other"): 5,
    ("stereo", "lossless"): 4,
    ("stereo", "dolby"): 3,
    ("stereo", "aac"): 2,
    ("stereo", "other"): 1,
}


def _codec_group(codec):
    """Bucket an ffprobe codec_name into lossless / dolby / aac / other."""
    if codec.startswith("truehd") or codec == "mlp" or codec.startswith("dts") or codec == "dca":
        return "lossless"
    if codec in _AUDIO_DOLBY:
        return "dolby"
    if codec.startswith("aac"):
        return "aac"
    return "other"


def _audio_dict(relation):
    """Find an ffprobe-style 'audio' dict via the SAME per-stream detail
    locations the video-tier waterfall uses (movies: 'detailed_info'; episodes:
    'info'.'info'). Tolerates audio stored as a list of tracks (takes the first).
    Returns None when no usable audio metadata exists (common until an
    advanced/detailed refresh has probed the stream)."""
    cp = _custom_props(relation)
    detailed_info = cp.get("detailed_info") if isinstance(cp.get("detailed_info"), dict) else None
    info = cp.get("info") if isinstance(cp.get("info"), dict) else None
    basic_data = cp.get("basic_data") if isinstance(cp.get("basic_data"), dict) else None
    dicts = [d for d in (detailed_info, info, basic_data) if d]
    if info is not None and isinstance(info.get("info"), dict):
        dicts.append(info["info"])
    for d in dicts:
        a = d.get("audio")
        if isinstance(a, list):
            a = next((x for x in a if isinstance(x, dict)), None)
        if isinstance(a, dict):
            return a
    return None


def _audio_group(a):
    """Classify an audio dict into (channel_group, codec_group)."""
    codec = str(a.get("codec_name") or "").strip().lower()
    layout = str(a.get("channel_layout") or "").strip().lower()
    try:
        ch = int(a.get("channels") or 0)
    except (TypeError, ValueError):
        ch = 0

    if ch >= 6 or "5.1" in layout or "6.1" in layout or "7.1" in layout:
        chan = "surround"
    elif ch == 2 or "stereo" in layout or layout == "2.0":
        chan = "stereo"
    else:
        chan = "other"

    return chan, _codec_group(codec)


def audio_rank(relation):
    """Rank a relation's audio (higher = better), surround-first then Dolby>AAC.

    Returns 0 when there's no usable audio info, so the tiebreak only ever
    reorders streams that actually carry an audio signal.
    """
    a = _audio_dict(relation)
    if not a:
        return 0
    return _AUDIO_RANKS.get(_audio_group(a), 0)


def _audio_label(relation):
    """Short 'codec/Nch' label for logging, or None when no audio info."""
    a = _audio_dict(relation)
    if not a:
        return None
    codec = str(a.get("codec_name") or "?").strip().lower()
    ch = a.get("channels")
    return f"{codec}/{ch}ch" if ch not in (None, "") else codec


# --------------------------------------------------------------------------- #
# Dolby Vision without a fallback layer
# --------------------------------------------------------------------------- #

def _iter_video_dicts(relation):
    """Yield the ffprobe-style 'video' dicts from the same per-stream detail
    locations the quality-tier waterfall reads (movies: 'detailed_info';
    episodes: 'info'.'info')."""
    cp = _custom_props(relation)
    detailed_info = cp.get("detailed_info") if isinstance(cp.get("detailed_info"), dict) else None
    info = cp.get("info") if isinstance(cp.get("info"), dict) else None
    basic_data = cp.get("basic_data") if isinstance(cp.get("basic_data"), dict) else None
    dicts = [d for d in (detailed_info, info, basic_data) if d]
    if info is not None and isinstance(info.get("info"), dict):
        dicts.append(info["info"])
    for d in dicts:
        v = d.get("video")
        if isinstance(v, dict):
            yield v


def _is_dv_no_fallback(relation) -> bool:
    """True when a stream is Dolby Vision WITHOUT an HDR10/SDR/HLG fallback layer
    -- i.e. it won't render correctly on non-DV devices (green/purple cast).

    The signal is a ffprobe 'DOVI configuration record' in the video track's
    side_data_list whose base-layer signal is compatible with nothing:
    dv_bl_signal_compatibility_id == 0 (Profile 5). Profiles 8.1/8.4/8.2 carry a
    HDR10/HLG/SDR base layer (compat 1/4/2) and are NOT flagged. Detection is
    positive-only: a stream with no DOVI record is never flagged, so untagged
    streams keep their native treatment.
    """
    for v in _iter_video_dicts(relation):
        for sd in (v.get("side_data_list") or []):
            if not isinstance(sd, dict):
                continue
            if "DOVI" not in str(sd.get("side_data_type") or ""):
                continue
            compat = sd.get("dv_bl_signal_compatibility_id")
            if compat == 0:
                return True
            # A Profile 5 record with a missing compat field is still no-fallback.
            if compat is None and sd.get("dv_profile") == 5:
                return True
    return False


# --------------------------------------------------------------------------- #
# Video bitrate (optional "avoid low-bitrate copies" rule)
# --------------------------------------------------------------------------- #

def _num(value):
    """A positive float from an int/float/numeric string, else None."""
    try:
        f = float(str(value).strip().split()[0])
    except Exception:
        return None
    return f if f > 0 else None


def _to_bps(value):
    """A file's TOTAL bitrate in bps. Providers store kbps, ffprobe stores bps."""
    n = _num(value)
    if n is None:
        return None
    return n * 1000.0 if n < _KBPS_CUTOFF else n


def _detail_dicts(relation):
    """The per-stream detail dicts, in the order the other readers use, plus
    vod_merge's own ffprobe record (movies) last."""
    cp = _custom_props(relation)
    info = cp.get("info") if isinstance(cp.get("info"), dict) else None
    out = [d for d in (cp.get("detailed_info"), info, cp.get("basic_data")) if isinstance(d, dict)]
    if info is not None and isinstance(info.get("info"), dict):
        out.append(info["info"])
    if isinstance(cp.get("ffprobe_info"), dict):
        out.append(cp["ffprobe_info"])
    return out


def _first_video(relation):
    for v in _iter_video_dicts(relation):
        if not _is_cover_image(v):
            return v
    return None


def _track_bps(track):
    """A single track's own bitrate in bps (its bit_rate, or an MKV BPS tag)."""
    if not isinstance(track, dict):
        return None
    tags = track.get("tags") if isinstance(track.get("tags"), dict) else {}
    for value in (tags.get("BPS"), tags.get("BPS-eng"), track.get("bit_rate")):
        bps = _num(value)
        if bps and bps >= _MIN_VIDEO_BPS:
            return bps
    return None


def video_bitrate_bps(relation):
    """Best estimate of the VIDEO bitrate in bps -> (bps, source) or (None, None).

    Prefers the video track's own figure. Otherwise uses the file's total minus
    its (first) audio track: totals include every audio track, and a copy with
    many language tracks can look far better than its video is. When no audio
    figure exists the total is used as-is, which can only overstate the video --
    the safe direction for a rule that demotes LOW bitrates.
    """
    track = _track_bps(_first_video(relation))
    if track:
        return track, "video"
    total = None
    for d in _detail_dicts(relation):
        if d.get("bitrate") not in (None, ""):
            total = _to_bps(d.get("bitrate"))
            if total:
                break
    if not total:
        return None, None
    audio = _audio_dict(relation)
    abps = None
    if isinstance(audio, dict):
        atags = audio.get("tags") if isinstance(audio.get("tags"), dict) else {}
        abps = _num(audio.get("bit_rate")) or _num(atags.get("BPS")) or _num(atags.get("BPS-eng"))
    if abps and total - abps >= _MIN_VIDEO_BPS:
        return total - abps, "total-audio"
    return total, "total"


def effective_mbps(relation):
    """Video bitrate in Mbps of H.264-equivalent quality, or None when unknown."""
    bps, _src = video_bitrate_bps(relation)
    if not bps:
        return None
    codec = str((_first_video(relation) or {}).get("codec_name") or "").strip().lower()
    return bps * _CODEC_EFFICIENCY.get(codec, 1.0) / 1e6


def is_starved(relation, floors) -> bool:
    """True when a copy's video bitrate is below its tier's minimum.

    Positive-only, like DV detection: unknown tier, unknown bitrate or no
    minimum for the tier -> False, so missing data never demotes a copy.
    """
    if not floors:
        return False
    floor = floors.get(quality_tier(relation))
    if not floor:
        return False
    mbps = effective_mbps(relation)
    return mbps is not None and mbps < floor


def bitrate_bucket(relation) -> int:
    """Coarse bitrate band (higher = better) for the final tiebreak; 0 = unknown.

    Bands are ~1.5x wide so near-identical encodes tie and keep native order.
    """
    mbps = effective_mbps(relation)
    if not mbps:
        return 0
    return max(1, 1 + int(math.floor(math.log(mbps * 1000.0) / math.log(_BITRATE_BUCKET_RATIO))))


def _bitrate_label(relation):
    """'4.58Mbps-eq' for logs, or None when unknown."""
    mbps = effective_mbps(relation)
    return None if mbps is None else "%.2fMbps-eq" % mbps


# --------------------------------------------------------------------------- #
# Category quality tag (Dispatcharr 0.33+)
# --------------------------------------------------------------------------- #

def _category_tags_exist() -> bool:
    """Does ANY category relation carry a quality tag? Cached for a few minutes,
    so an install with no tags pays nothing per request. Errors (older
    Dispatcharr, DB hiccup) read as "no" -- the rung simply stays dormant."""
    global _cat_tags_cache
    now = time.time()
    with _cat_tags_lock:
        if _cat_tags_cache is not None and now - _cat_tags_cache[1] < CATEGORY_TAGS_TTL_SECONDS:
            return _cat_tags_cache[0]
    try:
        from apps.vod.models import M3UVODCategoryRelation
        exists = M3UVODCategoryRelation.objects.filter(
            custom_properties__has_key="quality"
        ).exists()
    except Exception as exc:
        logger.debug("[VOD-PREF] category tag check failed (%s); treating as none", exc)
        exists = False
    with _cat_tags_lock:
        _cat_tags_cache = (bool(exists), time.time())
    return bool(exists)


def _category_tier_from_props(props):
    if not isinstance(props, dict):
        return None
    value = props.get("quality")
    if not isinstance(value, str):
        return None
    return _CATEGORY_QUALITY_TIERS.get(value.strip().lower())


def _annotate_category_tiers(candidates) -> None:
    """Attach each candidate's category quality tier (or None) for quality_tier().

    Movies carry their category directly; episodes through their series
    relation. At most two small queries per selection, and none at all while no
    category is tagged. Any failure leaves every candidate un-annotated (None),
    which is exactly today's behavior.
    """
    for c in candidates:
        try:
            setattr(c, _CAT_TIER_ATTR, None)
        except Exception:
            pass
    if not candidates or not _category_tags_exist():
        return
    try:
        from apps.vod.models import M3USeriesRelation, M3UVODCategoryRelation

        series_rel_ids = {getattr(c, "series_relation_id", None) for c in candidates} - {None}
        series_cat = {}
        if series_rel_ids:
            series_cat = dict(
                M3USeriesRelation.objects.filter(id__in=series_rel_ids).values_list("id", "category_id")
            )

        def cat_of(c):
            cat = getattr(c, "category_id", None)
            if cat is None:
                cat = series_cat.get(getattr(c, "series_relation_id", None))
            return cat

        wanted = {(c.m3u_account_id, cat_of(c)) for c in candidates}
        wanted = {pair for pair in wanted if pair[1] is not None}
        if not wanted:
            return
        rows = M3UVODCategoryRelation.objects.filter(
            m3u_account_id__in={a for a, _ in wanted},
            category_id__in={k for _, k in wanted},
        ).values_list("m3u_account_id", "category_id", "custom_properties")
        tiers = {(a, k): _category_tier_from_props(p) for a, k, p in rows}
        for c in candidates:
            setattr(c, _CAT_TIER_ATTR, tiers.get((c.m3u_account_id, cat_of(c))))
    except Exception as exc:
        logger.debug("[VOD-PREF] category tag lookup failed (%s); ignoring tags", exc)
        for c in candidates:
            try:
                setattr(c, _CAT_TIER_ATTR, None)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Title keys
# --------------------------------------------------------------------------- #

def _is_episode(content_obj) -> bool:
    # Episode has season/episode numbers + a series FK; Movie does not.
    return hasattr(content_obj, "season_number") and hasattr(content_obj, "episode_number")


def _episode_key(content_obj):
    series = getattr(content_obj, "series", None)
    suid = getattr(series, "uuid", None) if series is not None else None
    if suid is None:
        return None
    sn = getattr(content_obj, "season_number", None) or 0
    en = getattr(content_obj, "episode_number", None) or 0
    try:
        return f"ep:{suid}:S{int(sn):02d}E{int(en):02d}"
    except (TypeError, ValueError):
        return f"ep:{suid}:S{sn}E{en}"


def _title_level_keys(content_obj):
    """Stream-independent identity keys for a title, best (most stable) first."""
    keys = []
    tmdb = _nonempty(getattr(content_obj, "tmdb_id", None))
    if tmdb:
        keys.append(f"tmdb:{tmdb}")
    imdb = _nonempty(getattr(content_obj, "imdb_id", None))
    if imdb:
        keys.append(f"imdb:{imdb}")
    # The episode key touches content_obj.series (a lazy DB query, since the
    # native selector doesn't select_related it). Only compute it when there is
    # no tmdb/imdb -- capture already prefers keys[0], so the episode key is only
    # ever the storage/lookup key in the no-external-id case, and we avoid an
    # extra query on the common (has-tmdb) episode play path.
    if not keys and _is_episode(content_obj):
        ep = _episode_key(content_obj)
        if ep:
            keys.append(ep)
    return keys


def _stream_fallback_key(relation) -> str:
    return f"as:{relation.m3u_account_id}:{relation.stream_id}"


def _capture_key(content_obj, relation) -> str:
    """The key under which a MOVIE pick is stored (episodes use _series_key)."""
    keys = _title_level_keys(content_obj)
    if keys:
        return keys[0]
    return _stream_fallback_key(relation)


def _series_key(content_obj):
    """Show-level identity key for an episode's series, most-stable first.

    TV picks are remembered per SERIES, not per episode: picking a provider on
    one episode makes it the default provider for the whole show. Keyed on the
    series' tmdb/imdb (stable across the UUID-regenerating refresh) then the
    series uuid.
    """
    if not _is_episode(content_obj):
        return None
    series = getattr(content_obj, "series", None)
    if series is None:
        return None
    stmdb = _nonempty(getattr(series, "tmdb_id", None))
    if stmdb:
        return f"stmdb:{stmdb}"
    simdb = _nonempty(getattr(series, "imdb_id", None))
    if simdb:
        return f"simdb:{simdb}"
    suid = getattr(series, "uuid", None)
    if suid:
        return f"series:{suid}"
    return None


def _episode_lookup_keys(content_obj):
    """Keys to check when applying a saved pick to an episode, best first:
    series-level (the TV mechanism), then any episode-specific / legacy keys."""
    keys = []
    sk = _series_key(content_obj)
    if sk:
        keys.append(sk)
    # Episode-specific ids are rare, but honour them (and legacy per-episode
    # picks stored before show-level scoping) so nothing silently stops working.
    tmdb = _nonempty(getattr(content_obj, "tmdb_id", None))
    if tmdb:
        keys.append(f"tmdb:{tmdb}")
    imdb = _nonempty(getattr(content_obj, "imdb_id", None))
    if imdb:
        keys.append(f"imdb:{imdb}")
    ek = _episode_key(content_obj)
    if ek:
        keys.append(ek)
    return keys


# --------------------------------------------------------------------------- #
# Config + saved-pick access (DB-backed, TTL-cached)
# --------------------------------------------------------------------------- #

def _read_plugin_settings() -> dict:
    """PluginConfig.settings for this plugin (holds the user-facing fields)."""
    from apps.plugins.models import PluginConfig
    row = PluginConfig.objects.filter(key=PLUGIN_KEY).values("settings").first()
    if not row:
        return {}
    return row.get("settings") or {}


def _legacy_picks() -> dict:
    """Best-effort read of the pre-0.1.1 picks location (PluginConfig.settings)."""
    try:
        legacy = _read_plugin_settings().get(LEGACY_SAVED_PICKS_KEY)
        return dict(legacy) if isinstance(legacy, dict) else {}
    except Exception:
        return {}


def _read_picks_from_db() -> dict:
    """The saved-picks map from its CoreSettings row (migrating from legacy)."""
    from core.models import CoreSettings
    row = CoreSettings.objects.filter(key=PICKS_CORE_KEY).values("value").first()
    if row is not None:
        val = row.get("value")
        return dict(val) if isinstance(val, dict) else {}
    # No CoreSettings row yet: fall back to the legacy location so an existing
    # pick keeps working until the first write migrates it across.
    return _legacy_picks()


def _write_picks(mutate) -> int:
    """Apply *mutate(picks_dict)* to the CoreSettings row under a lock.

    *mutate* edits the dict in place and returns True if it changed anything.
    The row is seeded from the legacy location on first write (migration), and
    is always created if missing so later reads never fall back to legacy again.
    Returns the resulting pick count.
    """
    from django.db import transaction
    from core.models import CoreSettings

    with transaction.atomic():
        row = CoreSettings.objects.select_for_update().filter(key=PICKS_CORE_KEY).first()
        if row is None:
            picks = _legacy_picks()
            mutate(picks)
            CoreSettings.objects.create(
                key=PICKS_CORE_KEY, name=PICKS_CORE_NAME, value=picks
            )
        else:
            picks = dict(row.value) if isinstance(row.value, dict) else {}
            if mutate(picks):
                row.value = picks
                row.save(update_fields=["value"])
        count = len(picks)
    invalidate_config_cache()
    return count


def _read_avoid_from_db() -> list:
    """The avoid-rule list from its CoreSettings row ([] when absent)."""
    from core.models import CoreSettings
    row = CoreSettings.objects.filter(key=AVOID_CORE_KEY).values("value").first()
    val = (row or {}).get("value")
    rules = val.get("rules") if isinstance(val, dict) else None
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def _write_avoid(mutate) -> list:
    """Apply *mutate(rules_list)* to the avoid row under a lock (creating it on
    first write). *mutate* edits the list in place and returns True if it changed
    anything. Returns the resulting list."""
    from django.db import transaction
    from core.models import CoreSettings

    with transaction.atomic():
        row = CoreSettings.objects.select_for_update().filter(key=AVOID_CORE_KEY).first()
        if row is None:
            rules = []
            if mutate(rules):
                CoreSettings.objects.create(
                    key=AVOID_CORE_KEY, name=AVOID_CORE_NAME, value={"rules": rules}
                )
        else:
            val = row.value if isinstance(row.value, dict) else {}
            rules = [r for r in (val.get("rules") or []) if isinstance(r, dict)]
            if mutate(rules):
                row.value = {"rules": rules}
                row.save(update_fields=["value"])
    invalidate_config_cache()
    return rules


def _parse_floors(settings) -> dict:
    """{tier: Mbps} from the min_bitrate_* settings. Blank/invalid -> the default;
    0 or negative -> no minimum for that tier (omitted)."""
    floors = {}
    for tier, default in DEFAULT_MIN_BITRATE.items():
        raw = settings.get(MIN_BITRATE_SETTING[tier]) if isinstance(settings, dict) else None
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            value = default
        else:
            try:
                value = float(str(raw).strip())
            except (TypeError, ValueError):
                value = default
        if value > 0:
            floors[tier] = value
    return floors


def _load_config(force: bool = False) -> dict:
    """Return {prefer_quality, remember_ui_picks, saved_picks}, TTL-cached.

    Fields come from PluginConfig.settings (defaults applied here because the raw
    row is not default-merged for our out-of-band reads); picks come from their
    own CoreSettings row.
    """
    global _cfg_cache, _cfg_cache_ts
    now = time.time()
    with _cfg_lock:
        if not force and _cfg_cache is not None and (now - _cfg_cache_ts) < CONFIG_TTL_SECONDS:
            return _cfg_cache

    try:
        settings = _read_plugin_settings()
    except Exception as exc:
        logger.debug("[VOD-PREF] settings read failed (%s); using defaults", exc)
        settings = {}
    try:
        picks = _read_picks_from_db()
    except Exception as exc:
        logger.debug("[VOD-PREF] picks read failed (%s); treating as empty", exc)
        picks = {}
    try:
        avoid_rules = _read_avoid_from_db()
    except Exception as exc:
        logger.debug("[VOD-PREF] avoid rules read failed (%s); treating as none", exc)
        avoid_rules = []

    cfg = {
        "prefer_quality": settings.get("prefer_quality", DEFAULT_PREFER_QUALITY) or "off",
        "remember_ui_picks": _as_bool(
            settings.get("remember_ui_picks", DEFAULT_REMEMBER_UI_PICKS),
            DEFAULT_REMEMBER_UI_PICKS,
        ),
        "prefer_audio": _as_bool(
            settings.get("prefer_audio", DEFAULT_PREFER_AUDIO),
            DEFAULT_PREFER_AUDIO,
        ),
        "avoid_dv_no_fallback": _as_bool(
            settings.get("avoid_dv_no_fallback", DEFAULT_AVOID_DV_NO_FALLBACK),
            DEFAULT_AVOID_DV_NO_FALLBACK,
        ),
        "avoid_low_bitrate": _as_bool(
            settings.get("avoid_low_bitrate", DEFAULT_AVOID_LOW_BITRATE),
            DEFAULT_AVOID_LOW_BITRATE,
        ),
        "min_bitrate": _parse_floors(settings),
        "saved_picks": picks if isinstance(picks, dict) else {},
        "avoid_rules": avoid_rules,
        "notify_missed_4k": _as_bool(
            settings.get("notify_missed_4k", DEFAULT_NOTIFY_MISSED_4K),
            DEFAULT_NOTIFY_MISSED_4K,
        ),
        "prowl_api_key": str(settings.get("prowl_api_key") or "").strip(),
        "webhook_url": str(settings.get("webhook_url") or "").strip(),
    }
    with _cfg_lock:
        _cfg_cache = cfg
        _cfg_cache_ts = time.time()
    return cfg


def invalidate_config_cache() -> None:
    global _cfg_cache, _cfg_cache_ts
    with _cfg_lock:
        _cfg_cache = None
        _cfg_cache_ts = 0.0


def _persist_pick(content_obj, relation) -> None:
    """Remember this pick. Merge-safe; no-ops when the stored value is unchanged
    (so a client's open-file burst causes at most one write).

    Movies store the exact (account, stream). TV stores the PROVIDER for the
    whole series (account only, keyed by _series_key) -- picking a stream on one
    episode makes that provider the default for every episode of the show.
    """
    if _is_episode(content_obj):
        key = _series_key(content_obj)
        if key is None:  # series with no id/uuid (shouldn't happen) -> skip
            return
        rec = {"m3u_account_id": relation.m3u_account_id}
    else:
        key = _capture_key(content_obj, relation)
        rec = {"m3u_account_id": relation.m3u_account_id, "stream_id": str(relation.stream_id)}

    # Cheap pre-check against the cache to skip the common no-change burst case
    # without touching the DB at all.
    cached = _load_config()
    if cached["saved_picks"].get(key) == rec:
        return

    stored = {"ok": False}

    def _mutate(picks):
        if picks.get(key) == rec:
            return False  # another greenlet already wrote it under the lock
        if key not in picks and len(picks) >= MAX_SAVED_PICKS:
            logger.warning(
                "[VOD-PREF] saved-picks cap (%s) reached; not storing new key %s",
                MAX_SAVED_PICKS, key,
            )
            return False
        picks[key] = rec
        stored["ok"] = True
        return True

    try:
        _write_picks(_mutate)
        if stored["ok"]:
            tail = ("stream %s" % rec["stream_id"]) if "stream_id" in rec else "(whole series)"
            logger.info(
                "[VOD-PREF] remembered UI pick: %s -> account %s %s",
                key, rec["m3u_account_id"], tail,
            )
    except Exception as exc:
        logger.error("[VOD-PREF] failed to persist UI pick for %s: %s", key, exc)


# --------------------------------------------------------------------------- #
# Ladder application
# --------------------------------------------------------------------------- #

def _match_candidate(candidates, rec):
    """Find the candidate matching a saved record.

    A record with a stream_id (movie pick) matches that exact (account,) stream.
    A record with only m3u_account_id (show-level TV pick) matches the first
    candidate from that account -- i.e. "use this provider for this episode".
    """
    if not isinstance(rec, dict):
        return None
    want_stream = str(rec.get("stream_id")) if rec.get("stream_id") is not None else None
    want_account = rec.get("m3u_account_id")
    if want_stream is None and want_account is None:
        return None
    for c in candidates:
        if want_stream is not None and str(c.stream_id) != want_stream:
            continue
        if want_account is not None and c.m3u_account_id != want_account:
            continue
        return c
    return None


def _lookup_saved(content_obj, candidates, saved_picks):
    """Return the candidate a saved pick points to for this title, or None."""
    if not saved_picks:
        return None
    if _is_episode(content_obj):
        # Show-level pick (then episode-specific / legacy) -- see _episode_lookup_keys.
        lookup_keys = _episode_lookup_keys(content_obj)
    else:
        lookup_keys = _title_level_keys(content_obj)
    for key in lookup_keys:
        rec = saved_picks.get(key)
        if rec:
            chosen = _match_candidate(candidates, rec)
            if chosen is not None:
                return chosen
    # Movie stream-level fallback: any candidate whose own stream was the pick.
    if not _is_episode(content_obj):
        for c in candidates:
            if saved_picks.get(_stream_fallback_key(c)):
                return c
    return None


def _front(candidates, chosen):
    """chosen first, then the rest in their existing order (dedup by id)."""
    return [chosen] + [c for c in candidates if c.id != chosen.id]


def _log_label(content_obj, relation):
    """A cheap title label for log lines (no DB query -- avoids touching series)."""
    tmdb = _nonempty(getattr(content_obj, "tmdb_id", None))
    if tmdb:
        return f"tmdb:{tmdb}"
    imdb = _nonempty(getattr(content_obj, "imdb_id", None))
    if imdb:
        return f"imdb:{imdb}"
    return f"as:{relation.m3u_account_id}:{relation.stream_id}"


def _rule_season(rule):
    try:
        return int(rule.get("season")) if rule.get("season") is not None else None
    except (TypeError, ValueError):
        return None


def _avoided_ids(content_obj, candidates, rules) -> set:
    """Relation ids of the candidates an avoid rule covers for this title.

    Episode rules name a PROVIDER for a show (optionally one season), keyed like
    a show-level saved pick (_series_key). Movie rules name one exact copy
    (account + stream) of a title. The series lookup is only paid when at least
    one episode rule exists.
    """
    if not rules or not candidates:
        return set()
    if _is_episode(content_obj):
        ep_rules = [r for r in rules if r.get("kind") == "episode"]
        if not ep_rules:
            return set()
        sk = _series_key(content_obj)
        if sk is None:
            return set()
        try:
            season = int(getattr(content_obj, "season_number", None))
        except (TypeError, ValueError):
            season = None
        accts = set()
        for r in ep_rules:
            if r.get("key") != sk:
                continue
            rs = _rule_season(r)
            if rs is not None and rs != season:
                continue
            accts.add(r.get("acct"))
        return {c.id for c in candidates if c.m3u_account_id in accts}
    mv_rules = [r for r in rules if r.get("kind") == "movie"]
    if not mv_rules:
        return set()
    title_keys = set(_title_level_keys(content_obj))
    out = set()
    for c in candidates:
        for r in mv_rules:
            if (c.m3u_account_id == r.get("acct") and str(c.stream_id) == str(r.get("stream"))
                    and (r.get("key") in title_keys or r.get("key") == _stream_fallback_key(c))):
                out.add(c.id)
                break
    return out


def _demote(candidates, avoided):
    """Candidates with the avoided ones moved to the end (both halves keep order)."""
    if not avoided:
        return list(candidates)
    return ([c for c in candidates if c.id not in avoided]
            + [c for c in candidates if c.id in avoided])


def _quality_ranked(candidates, prefer, use_audio=False, avoid_dv=False, floors=None,
                    avoided=None):
    """Stable-sort candidates (best first) by a composite key.

    Key = (compatible, not_avoided, quality_rank, not_starved, audio_rank,
    bitrate_bucket), all higher-is-better:
      * not_avoided -- (second) a copy an avoid rule covers scores 0, so it ranks
        after every other PLAYABLE copy (it stays in the failover list). It sits
        below `compatible` because an avoid rule means "anything else that
        plays", and a no-fallback DV copy doesn't play properly where that
        setting is on.
      * compatible -- when *avoid_dv* is on, a Dolby-Vision-without-fallback
        stream scores 0 and everything else 1, so a PLAYABLE stream outranks a
        no-fallback DV stream even of higher resolution. Off -> all 1 (no effect).
      * quality_rank -- the prefer_quality ladder (0 when prefer is off/unset).
      * not_starved -- when *floors* is given (avoid-low-bitrate on) and a
        quality preference is active: a copy below its tier's minimum bitrate
        scores 0, so within one tier a healthy copy wins. It never crosses
        tiers (it sits AFTER quality_rank). Unknown bitrate -> 1 (not starved).
      * audio_rank -- tiebreak within a video tier, only when *use_audio* and a
        quality preference are both active.
      * bitrate_bucket -- LAST tiebreak (same activation as not_starved): a
        clearly higher bitrate wins; near-identical ones tie; unknown -> 0.
    The sort is stable, so streams equal on every active key keep native order.
    """
    quality_on = bool(prefer) and prefer != "off"
    priority = _QUALITY_PRIORITY.get(prefer, _QUALITY_PRIORITY["4k"]) if quality_on else None
    use_bitrate = quality_on and floors is not None
    avoided = avoided or ()
    return sorted(
        candidates,
        key=lambda c: (
            0 if (avoid_dv and _is_dv_no_fallback(c)) else 1,
            0 if c.id in avoided else 1,
            quality_rank(c, priority) if priority else 0,
            0 if (use_bitrate and is_starved(c, floors)) else 1,
            audio_rank(c) if (quality_on and use_audio) else 0,
            bitrate_bucket(c) if use_bitrate else 0,
        ),
        reverse=True,
    )


def _finalize_show_pick(chosen, candidates, prefer, use_audio=False, avoid_dv=False, floors=None,
                        avoided=None):
    """A TV show pick names a PROVIDER (account). Keep that provider, but still
    honour the quality rule and DV-avoidance WITHIN it -- so when one provider
    carries both the 4K and non-4K copy of an episode, Prefer 4K picks the 4K
    stream, and a compatible copy is preferred over a no-fallback DV one. Fail
    over to other providers (native order, avoided ones last) only if the chosen
    provider can't serve it. An explicit pick outranks an avoid rule.
    """
    acct = chosen.m3u_account_id
    same = [c for c in candidates if c.m3u_account_id == acct]
    other = _demote([c for c in candidates if c.m3u_account_id != acct], avoided)
    if same and ((prefer and prefer != "off") or avoid_dv):
        same = _quality_ranked(same, prefer, use_audio, avoid_dv, floors)
    primary = same[0] if same else chosen
    return primary, same + other


def _apply_preferences(content_obj, relation, candidates,
                       preferred_stream_id, preferred_m3u_account_id):
    """Return (new_relation, new_candidates, reason). `reason` is for logging."""
    cfg = _load_config()
    remember = cfg["remember_ui_picks"]
    prefer = cfg["prefer_quality"]
    use_audio = cfg["prefer_audio"]
    avoid_dv = cfg["avoid_dv_no_fallback"]
    floors = cfg["min_bitrate"] if cfg.get("avoid_low_bitrate") else None

    # 1. Explicit request pick: the caller asked for a specific stream OR a
    #    specific account and the original honoured it. The movie UI sends
    #    stream_id; the SERIES UI sends m3u_account_id instead (a series-level
    #    provider has no per-episode stream_id), so we must accept both -- the
    #    chosen relation still resolves to a concrete (account, stream) to store.
    honoured_stream = bool(preferred_stream_id) and str(relation.stream_id) == str(preferred_stream_id)
    honoured_account = bool(preferred_m3u_account_id) and relation.m3u_account_id == preferred_m3u_account_id
    if honoured_stream or honoured_account:
        if remember:
            _persist_pick(content_obj, relation)
        return relation, candidates, "request-pick"

    # Category quality tags feed the quality waterfall; fetch them once for the
    # whole candidate list (free when no category is tagged).
    quality_on = bool(prefer) and prefer != "off"
    if quality_on:
        _annotate_category_tiers(candidates)

    # Avoid rules (user-marked bad copies). They DEMOTE -- an avoided copy ranks
    # after every other copy but stays in the failover list, so a title whose
    # only copies are avoided still plays. A lookup failure means "no rules".
    try:
        avoided = _avoided_ids(content_obj, candidates, cfg.get("avoid_rules"))
    except Exception as exc:
        logger.debug("[VOD-PREF] avoid-rule lookup failed: %s", exc)
        avoided = set()

    # 2. Saved UI pick for this title. Movies pin the exact stream; TV pins the
    #    provider for the whole series and still honours the quality rule within it.
    #    An explicit pick outranks an avoid rule; the failover tail still demotes.
    if remember:
        chosen = _lookup_saved(content_obj, candidates, cfg["saved_picks"])
        if chosen is not None:
            if _is_episode(content_obj):
                primary, ordered = _finalize_show_pick(chosen, candidates, prefer, use_audio,
                                                       avoid_dv, floors, avoided)
            else:
                primary, ordered = chosen, _front(_demote(candidates, avoided), chosen)
            return primary, ordered, "saved-pick"

    # 3. Dolby-Vision avoidance, avoid rules, then the quality rule. All
    #    re-order candidates. DV-avoidance (when on) is the TOP sort key, so a
    #    playable stream beats a no-fallback DV one even at higher resolution;
    #    avoid rules come next (a user-marked bad copy still beats one that won't
    #    play properly); the quality rule and audio tiebreak order the rest.
    #    Audio can also decide when there's no video signal but an audio one is
    #    present; otherwise a no-signal quality result (and a DV-avoidance that
    #    changed nothing) keeps native order (avoided copies still moved last).
    if quality_on or avoid_dv or avoided:
        priority = _QUALITY_PRIORITY.get(prefer, _QUALITY_PRIORITY["4k"]) if quality_on else None
        ranked = _quality_ranked(candidates, prefer, use_audio, avoid_dv, floors, avoided)
        top = ranked[0] if ranked else None
        if top is not None:
            has_quality = quality_on and quality_rank(top, priority) > 0
            has_audio = quality_on and use_audio and audio_rank(top) > 0
            dv_moved = avoid_dv and _is_dv_no_fallback(relation) and not _is_dv_no_fallback(top)
            rule_moved = relation.id in avoided and top.id not in avoided
            if has_quality or has_audio:
                return top, ranked, "quality:%s" % prefer
            if dv_moved:
                return top, ranked, "avoid-dv"
            if rule_moved:
                return top, ranked, "avoid-rule"
        base = _demote(candidates, avoided)
        if quality_on:
            return relation, base, "quality:%s:no-signal" % prefer
        if avoided:
            return relation, base, "avoid-rule:failover"

    # 4. Native.
    return relation, candidates, "native"


# --------------------------------------------------------------------------- #
# Recent plays (for avoid rules) -- request path, best effort
# --------------------------------------------------------------------------- #

def _recent_entry(content_type, content_obj, relation):
    cid = getattr(content_obj, "id", None)
    if cid is None or relation is None:
        return None
    kind = "episode" if _is_episode(content_obj) else "movie"
    return {"type": kind, "cid": cid, "acct": relation.m3u_account_id,
            "stream": str(relation.stream_id), "ts": time.time()}


def _same_play(a, b) -> bool:
    """b is a repeat of a: same title and copy, within RECENT_REPEAT_SECONDS."""
    try:
        return ((a.get("type"), a.get("cid"), a.get("acct"), a.get("stream"))
                == (b["type"], b["cid"], b["acct"], b["stream"])
                and b["ts"] - float(a.get("ts") or 0) < RECENT_REPEAT_SECONDS)
    except Exception:
        return False


def _record_recent_play(content_type, content_obj, relation) -> bool:
    """Push the copy just served onto the recent-plays list. Never raises.

    Distinct TITLES: a newer play of a title replaces its older entry. A repeat
    of the newest entry (same title and copy within RECENT_REPEAT_SECONDS) is the
    same play and costs one Redis read. Returns True for a NEW play (the
    played-shows record is only written then); without Redis, repeats are
    detected per process.
    """
    try:
        entry = _recent_entry(content_type, content_obj, relation)
        if entry is None:
            return False
        r = _get_redis()
        if r is None:
            prev = _last_play_local.get("last")
            _last_play_local["last"] = entry
            return not (prev and _same_play(prev, entry))
        old = []
        for raw in r.lrange(RECENT_REDIS_KEY, 0, RECENT_MAX) or []:
            rec = _parse_worker_record(raw)
            if rec is not None:
                old.append(rec)
        if old and _same_play(old[0], entry):
            return False
        keep = [entry] + [e for e in old
                          if (e.get("type"), e.get("cid")) != (entry["type"], entry["cid"])]
        pipe = r.pipeline()
        pipe.delete(RECENT_REDIS_KEY)
        pipe.rpush(RECENT_REDIS_KEY, *[json.dumps(e) for e in keep[:RECENT_MAX]])
        pipe.execute()
        return True
    except Exception as exc:
        logger.debug("[VOD-PREF] recent-play record failed: %s", exc)
        return False


def _record_played_series(content_obj) -> None:
    """Update the durable played-shows record for an episode play. Never raises."""
    try:
        if not _is_episode(content_obj):
            return
        sid = getattr(content_obj, "series_id", None)
        if sid is None:
            sid = getattr(getattr(content_obj, "series", None), "id", None)
        if sid is None:
            return
        season = int(getattr(content_obj, "season_number", None) or 0)
        episode = int(getattr(content_obj, "episode_number", None) or 0)
        now = time.time()

        def _mutate(series):
            prev = series.get(str(sid)) if isinstance(series.get(str(sid)), dict) else {}
            furthest = max((int(prev.get("max_season", -1)), int(prev.get("max_episode", -1))),
                           (season, episode))
            series[str(sid)] = {
                "ts": now, "season": season, "episode": episode,
                "episode_id": getattr(content_obj, "id", None),
                "max_season": furthest[0], "max_episode": furthest[1],
            }
            cutoff = now - PLAYED_RETENTION_DAYS * 86400
            for k in [k for k, v in series.items()
                      if not isinstance(v, dict) or float(v.get("ts") or 0) < cutoff]:
                del series[k]
            return True

        _write_played(_mutate)
    except Exception as exc:
        logger.debug("[VOD-PREF] played-shows record failed: %s", exc)


def _write_played(mutate) -> dict:
    """Apply *mutate(series_dict)* to the played-shows row under a lock."""
    from django.db import transaction
    from core.models import CoreSettings

    with transaction.atomic():
        row = CoreSettings.objects.select_for_update().filter(key=PLAYED_CORE_KEY).first()
        val = (row.value if row is not None and isinstance(row.value, dict) else {}) or {}
        series = dict(val.get("series") or {}) if isinstance(val.get("series"), dict) else {}
        if not mutate(series):
            return series
        new_val = {"version": PLAYED_FORMAT_VERSION, "series": series}
        if row is None:
            CoreSettings.objects.create(key=PLAYED_CORE_KEY, name=PLAYED_CORE_NAME, value=new_val)
        else:
            row.value = new_val
            row.save(update_fields=["value"])
    return series


def get_played_series() -> dict:
    """{series_id(str): record} from the played-shows row ({} when absent)."""
    try:
        from core.models import CoreSettings
        row = CoreSettings.objects.filter(key=PLAYED_CORE_KEY).values("value").first()
        val = (row or {}).get("value")
        series = val.get("series") if isinstance(val, dict) else None
        return dict(series) if isinstance(series, dict) else {}
    except Exception as exc:
        logger.debug("[VOD-PREF] played-shows read failed: %s", exc)
        return {}


def get_recent_plays() -> list:
    """The recent-plays entries, newest first ([] when unavailable)."""
    r = _get_redis()
    if r is None:
        return []
    out = []
    for raw in r.lrange(RECENT_REDIS_KEY, 0, RECENT_MAX - 1) or []:
        rec = _parse_worker_record(raw)
        if rec is not None:
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# "Missed 4K" notification -- request path decides, a background thread sends
# --------------------------------------------------------------------------- #

def _is_missed_4k(cfg, reason, chosen, candidates) -> bool:
    """True when the plugin served a non-4K copy because every 4K copy is Dolby
    Vision without an HDR/SDR fallback (Profile 5) -- i.e. DV avoidance cost the
    viewer 4K. Explicit picks (request or saved) are the user's own choice and
    never alert."""
    if not (cfg.get("notify_missed_4k") and cfg.get("avoid_dv_no_fallback")
            and cfg.get("prefer_quality") == "4k"):
        return False
    if reason in ("request-pick", "saved-pick", "native"):
        return False
    if quality_tier(chosen) == "4k":
        return False
    four_k = [c for c in candidates if quality_tier(c) == "4k"]
    return bool(four_k) and all(_is_dv_no_fallback(c) for c in four_k)


def _content_label(content_obj) -> str:
    """'Show Name S02E03' / 'Movie Name (2021)'. May query the series (episodes);
    only called when an alert actually fires."""
    if _is_episode(content_obj):
        series = getattr(content_obj, "series", None)
        name = getattr(series, "name", None) or "Unknown show"
        try:
            return "%s S%02dE%02d" % (name, int(content_obj.season_number or 0),
                                      int(content_obj.episode_number or 0))
        except (TypeError, ValueError):
            return str(name)
    name = getattr(content_obj, "name", None) or "Unknown movie"
    year = getattr(content_obj, "year", None)
    return "%s (%s)" % (name, year) if year else str(name)


def _account_name(relation) -> str:
    acct = getattr(relation, "m3u_account", None)
    return getattr(acct, "name", None) or "account %s" % relation.m3u_account_id


def _claim_notification(dedupe_key) -> bool:
    """True if this process may send the alert for *dedupe_key* (first in the
    window). Redis SET NX across workers; an in-process dict without Redis."""
    r = _get_redis()
    if r is not None:
        try:
            return bool(r.set(NOTIFY_DEDUPE_PREFIX + dedupe_key, "1",
                              nx=True, ex=int(NOTIFY_DEDUPE_SECONDS)))
        except Exception:
            pass
    now = time.time()
    last = _notified_local.get(dedupe_key)
    if last is not None and now - last < NOTIFY_DEDUPE_SECONDS:
        return False
    _notified_local[dedupe_key] = now
    return True


def send_notification(prowl_api_key, webhook_url, title, message) -> list:
    """Send *title*/*message* to Prowl and/or a webhook. Blocking, short timeout,
    never raises. Returns [(channel, ok, detail), ...] (empty if none configured).

    The webhook receives a JSON POST: {"event", "title", "message", "text",
    "content"} -- `text`/`content` let Slack/Discord/Mattermost-style webhooks
    show it as-is.
    """
    results = []
    if not prowl_api_key and not webhook_url:
        return results
    try:
        import requests
    except Exception as exc:
        return [("setup", False, "requests unavailable: %s" % exc)]
    if prowl_api_key:
        try:
            resp = requests.post(PROWL_URL, data={
                "apikey": prowl_api_key, "application": NOTIFY_APP_NAME,
                "event": title, "description": message,
            }, timeout=NOTIFY_TIMEOUT_SECONDS)
            ok = resp.status_code == 200
            results.append(("prowl", ok, "HTTP %s" % resp.status_code))
        except Exception as exc:
            results.append(("prowl", False, type(exc).__name__))
    if webhook_url:
        try:
            full = "%s: %s" % (title, message)
            resp = requests.post(webhook_url, json={
                "event": "missed_4k" if title == "Missed 4K" else "vod_preferences",
                "title": title, "message": message, "text": full, "content": full,
            }, timeout=NOTIFY_TIMEOUT_SECONDS)
            ok = 200 <= resp.status_code < 300
            results.append(("webhook", ok, "HTTP %s" % resp.status_code))
        except Exception as exc:
            results.append(("webhook", False, type(exc).__name__))
    for channel, ok, detail in results:
        if not ok:
            logger.warning("[VOD-PREF] %s notification failed (%s)", channel, detail)
    return results


def _notify_async(cfg, title, message) -> None:
    """Send in a daemon thread (a greenlet under Dispatcharr's gevent uWSGI), so
    the stream request never waits on a notification service."""
    sender = _notify_sender or send_notification
    args = (cfg.get("prowl_api_key"), cfg.get("webhook_url"), title, message)
    t = threading.Thread(target=sender, args=args, daemon=True,
                         name="vodpref-notify")
    t.start()


def _maybe_notify_missed_4k(cfg, reason, content_type, content_obj, chosen, candidates) -> None:
    """Fire the 'missed 4K' alert when it applies. Never raises."""
    try:
        if not (cfg.get("prowl_api_key") or cfg.get("webhook_url")):
            return
        if not _is_missed_4k(cfg, reason, chosen, candidates):
            return
        cid = getattr(content_obj, "id", None)
        if cid is None or not _claim_notification("%s:%s" % (content_type, cid)):
            return
        tier = quality_tier(chosen) or "unknown quality"
        message = ("%s played %s from %s; its only 4K copy is Dolby Vision without "
                   "an HDR/SDR fallback (Profile 5)."
                   % (_content_label(content_obj), tier, _account_name(chosen)))
        logger.info("[VOD-PREF] missed 4K: %s", message)
        _notify_async(cfg, "Missed 4K", message)
    except Exception as exc:
        logger.debug("[VOD-PREF] missed-4K notification skipped: %s", exc)


# --------------------------------------------------------------------------- #
# Patched: _get_content_and_relation
# --------------------------------------------------------------------------- #

def patched_get_content_and_relation(content_type, content_id,
                                     preferred_m3u_account_id=None,
                                     preferred_stream_id=None, *args, **kwargs):
    """Signature-agnostic wrapper: forward any extra positional/keyword args
    straight through to the original at EVERY call site.

    Why: a future Dispatcharr release could add a parameter to
    `_get_content_and_relation` and pass it (possibly as a keyword). A fixed
    signature would raise TypeError while *binding arguments* -- before the
    `_ACTIVE` guard and before the try/except below -- and the caller wraps its
    body in a blanket `except` that returns HTTP 500, so a signature change would
    turn "the plugin quietly stopped helping" into a hard failure on every VOD
    request. Fail-open covers what happens INSIDE the wrapper; only forwarding
    covers what happens before it is entered.

    Correctness note (this wrapper only REORDERS): it never fetches or adds
    candidates -- it re-ranks the exact candidate list the original returns, and
    reads only the two known preferred_* params. So any filter/restriction the
    original applies (a future param included) is already baked into that list and
    is preserved by reordering; there is nothing a new parameter could make us
    undo. Forwarding is therefore sufficient today -- nothing new to honour.
    """
    if not _ACTIVE:
        return _orig_get_content_and_relation(
            content_type, content_id, preferred_m3u_account_id, preferred_stream_id,
            *args, **kwargs
        )

    _log_pid_once("select")

    content_obj, relation, candidates = _orig_get_content_and_relation(
        content_type, content_id, preferred_m3u_account_id, preferred_stream_id,
        *args, **kwargs
    )

    try:
        if content_obj is None or relation is None or not candidates:
            return content_obj, relation, candidates
        new_relation, new_candidates, reason = _apply_preferences(
            content_obj, relation, candidates,
            preferred_stream_id, preferred_m3u_account_id,
        )
        # One decisive line per selection (DEBUG -- enable debug logging to watch
        # the plugin work; quiet at the default INFO level). Fires even when the
        # choice AGREES with native account priority (reason set, changed=False);
        # "native" (feature effectively off) stays quiet.
        if reason != "native":
            changed = new_relation.id != relation.id
            logger.debug(
                "[VOD-PREF] %s %s: %s -> account %s stream %s (tier=%s, audio=%s, "
                "bitrate=%s, dv_nofallback=%s, changed=%s, candidates=%d)",
                content_type, _log_label(content_obj, new_relation), reason,
                new_relation.m3u_account_id, new_relation.stream_id,
                quality_tier(new_relation), _audio_label(new_relation),
                _bitrate_label(new_relation),
                _is_dv_no_fallback(new_relation), changed, len(new_candidates),
            )
        # Side effects that never change the choice (each fails silently, and
        # this guard keeps any surprise from undoing the selection above).
        try:
            if _record_recent_play(content_type, content_obj, new_relation):
                _record_played_series(content_obj)
            _maybe_notify_missed_4k(_load_config(), reason, content_type, content_obj,
                                    new_relation, new_candidates)
        except Exception as exc:
            logger.debug("[VOD-PREF] post-selection bookkeeping failed: %s", exc)
        return content_obj, new_relation, new_candidates
    except Exception as exc:
        logger.error("[VOD-PREF] preference logic error, using native selection: %s", exc)
        return content_obj, relation, candidates


# --------------------------------------------------------------------------- #
# Per-worker install registry (for a truthful Status action)
# --------------------------------------------------------------------------- #

def _process_role() -> str:
    """'uwsgi' for a stream-serving uWSGI worker, 'celery' for a Celery process,
    else 'other'. Mirrors Dispatcharr's own process labelling."""
    try:
        import uwsgi  # only importable inside uWSGI
        if uwsgi.worker_id() > 0:
            return "uwsgi"
    except Exception:
        pass
    import sys
    argv = sys.argv or []
    if any("celery" in str(a) for a in argv):
        return "celery"
    return "other"


def _get_redis():
    try:
        from core.utils import RedisClient
        return RedisClient.get_client()
    except Exception:
        return None


def _worker_field() -> str:
    import socket
    return "%s:%s" % (socket.gethostname(), os.getpid())


def _register_worker(active: bool) -> None:
    """Record this process's patch state in Redis. Best effort, never raises.

    Only uWSGI workers serve VOD, so they're what Status reports; other roles
    are recorded too so Status can say where IT ran. Stale records (older than
    WORKER_RECORD_MAX_AGE, or a dead pid on this host) are pruned here.
    """
    try:
        r = _get_redis()
        if r is None:
            return
        import socket
        now = time.time()
        r.hset(WORKERS_REDIS_KEY, _worker_field(), json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "role": _process_role(),
            "active": bool(active), "ts": now,
        }))
        for field, raw in (r.hgetall(WORKERS_REDIS_KEY) or {}).items():
            rec = _parse_worker_record(raw)
            if rec is None or now - rec.get("ts", 0) > WORKER_RECORD_MAX_AGE or not _pid_alive(rec):
                r.hdel(WORKERS_REDIS_KEY, field)
    except Exception as exc:
        logger.debug("[VOD-PREF] worker registry write failed: %s", exc)


def _parse_worker_record(raw):
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def _pid_alive(rec) -> bool:
    """False only when the record is from THIS host and its pid is gone. A record
    from another host (separate container) can't be checked -> assumed alive."""
    if os.name != "posix":
        # Signal 0 is a liveness probe only on POSIX; on Windows os.kill would
        # TERMINATE the process. Dispatcharr runs on Linux; this guards tests.
        return True
    try:
        import socket
        if rec.get("host") != socket.gethostname():
            return True
        os.kill(int(rec.get("pid")), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True  # e.g. PermissionError: the pid exists


def worker_report() -> dict:
    """Summarize the registry for the Status action.

    -> {"available": bool, "active": [pids], "inactive": [pids], "here": role}
    "available" is False when Redis couldn't be read (Status then says so).
    """
    out = {"available": False, "active": [], "inactive": [], "here": _process_role()}
    try:
        r = _get_redis()
        if r is None:
            return out
        raw = r.hgetall(WORKERS_REDIS_KEY) or {}
        out["available"] = True
        for value in raw.values():
            rec = _parse_worker_record(value)
            if not rec or rec.get("role") != "uwsgi" or not _pid_alive(rec):
                continue
            (out["active"] if rec.get("active") else out["inactive"]).append(rec.get("pid"))
        out["active"].sort()
        out["inactive"].sort()
    except Exception as exc:
        logger.debug("[VOD-PREF] worker registry read failed: %s", exc)
    return out


# --------------------------------------------------------------------------- #
# Install / uninstall
# --------------------------------------------------------------------------- #

def install() -> bool:
    """Install the monkeypatch. Idempotent and reload-safe."""
    global _orig_get_content_and_relation, _ACTIVE

    try:
        from apps.proxy.vod_proxy import views as vod_views
    except Exception as exc:
        logger.error("[VOD-PREF] could not import Dispatcharr VOD views: %s", exc)
        return False

    if not hasattr(vod_views, "_get_content_and_relation"):
        logger.error("[VOD-PREF] views._get_content_and_relation missing -- not patching.")
        return False

    try:
        cur = vod_views._get_content_and_relation
        if not getattr(cur, _PATCH_TAG, False):
            _orig_get_content_and_relation = cur

        setattr(patched_get_content_and_relation, _PATCH_TAG, True)
        vod_views._get_content_and_relation = patched_get_content_and_relation

        _ACTIVE = True
        logger.info(
            "[VOD-PREF] installed VOD preferences patch in worker pid=%s", os.getpid()
        )
        _register_worker(True)
        return True
    except Exception as exc:
        logger.exception("[VOD-PREF] install failed: %s", exc)
        uninstall()
        return False


def uninstall() -> bool:
    """Revert the monkeypatch (best effort) and deactivate."""
    global _ACTIVE
    _ACTIVE = False
    try:
        from apps.proxy.vod_proxy import views as vod_views
    except Exception:
        return False
    try:
        if _orig_get_content_and_relation is not None:
            vod_views._get_content_and_relation = _orig_get_content_and_relation
        logger.info("[VOD-PREF] uninstalled patch in worker pid=%s", os.getpid())
        _register_worker(False)
        return True
    except Exception as exc:
        logger.error("[VOD-PREF] uninstall error: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Saved-pick management (used by the plugin's config actions)
# --------------------------------------------------------------------------- #

def get_saved_picks() -> dict:
    """Return the current saved-picks map straight from the DB (uncached)."""
    try:
        return _read_picks_from_db()
    except Exception as exc:
        logger.error("[VOD-PREF] could not read saved picks: %s", exc)
        return {}


def clear_all_saved() -> int:
    """Remove every saved pick. Returns how many were removed."""
    removed = {"n": 0}

    def _mutate(picks):
        removed["n"] = len(picks)
        if not picks:
            return False
        picks.clear()
        return True

    _write_picks(_mutate)
    return removed["n"]


def clear_saved_key(raw) -> bool:
    """Remove one saved pick by title key or by tmdb/imdb id. Returns True if removed."""
    raw = _nonempty(raw)
    if not raw:
        return False
    # Accept an exact stored key, or a bare id we normalise to the possible
    # movie (tmdb/imdb) and show-level (series tmdb/imdb) key forms.
    candidates_keys = [raw]
    if not raw.startswith(("tmdb:", "imdb:", "stmdb:", "simdb:", "series:", "ep:", "as:")):
        candidates_keys += [f"tmdb:{raw}", f"imdb:{raw}", f"stmdb:{raw}", f"simdb:{raw}"]

    hit = {"removed": False}

    def _mutate(picks):
        for k in candidates_keys:
            if k in picks:
                del picks[k]
                hit["removed"] = True
        return hit["removed"]

    _write_picks(_mutate)
    return hit["removed"]


# --------------------------------------------------------------------------- #
# Recent plays + avoid-rule management (used by the plugin's config actions)
# --------------------------------------------------------------------------- #

def _short(text, limit=RECENT_LABEL_CHARS) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_EP_SUFFIX_RE = re.compile(r"\s(S\d+E\d+)$")


def _short_label(label, limit=RECENT_LABEL_CHARS) -> str:
    """Like _short, but an episode's trailing 'SxxEyy' always survives: only the
    show name is shortened."""
    label = str(label or "").strip()
    m = _EP_SUFFIX_RE.search(label)
    if not m or len(label) <= limit:
        return _short(label, limit)
    suffix = m.group(1)
    return _short(label[: m.start()], max(8, limit - len(suffix) - 1)) + " " + suffix


def _ago(ts) -> str:
    try:
        secs = max(0, time.time() - float(ts))
    except (TypeError, ValueError):
        return "?"
    if secs < 3600:
        return "%dm" % max(1, secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def _account_names(ids) -> dict:
    try:
        from apps.m3u.models import M3UAccount
        return dict(M3UAccount.objects.filter(id__in=list(ids)).values_list("id", "name"))
    except Exception:
        return {}


def _resolve_play(entry):
    """(content_obj, label) for a recent-play entry, or (None, reason)."""
    try:
        from apps.vod.models import Episode, Movie
        if entry.get("type") == "episode":
            obj = Episode.objects.select_related("series").filter(id=entry.get("cid")).first()
        else:
            obj = Movie.objects.filter(id=entry.get("cid")).first()
    except Exception as exc:
        return None, "lookup failed (%s)" % exc
    if obj is None:
        return None, "title no longer in the library"
    return obj, _content_label(obj)


def describe_recent_plays() -> list:
    """[{n, label, account, ago, entry}] newest first, for the List action."""
    plays = get_recent_plays()
    names = _account_names({p.get("acct") for p in plays})
    out = []
    for n, p in enumerate(plays, 1):
        _obj, label = _resolve_play(p)
        out.append({
            "n": n, "label": label,
            "account": names.get(p.get("acct")) or "account %s" % p.get("acct"),
            "ago": _ago(p.get("ts")), "entry": p,
        })
    return out


def format_recent_plays(rows) -> str:
    """One compact line (the action popup is narrow and may not keep newlines)."""
    if not rows:
        return ("No recent plays recorded yet. Play something through the proxy, "
                "then list again.")
    parts = ["#%d %s \u00b7 %s (%s)" % (r["n"], _short_label(r["label"]),
                                       _short(r["account"], 16), r["ago"])
             for r in rows]
    return "Recent plays (newest first): " + "  |  ".join(parts)


def _rule_text(rule) -> str:
    label = _short(rule.get("label") or rule.get("key"))
    acct = _short(rule.get("acct_name") or "account %s" % rule.get("acct"), 18)
    if rule.get("kind") == "episode":
        scope = "S%s" % rule.get("season") if rule.get("season") is not None else "all seasons"
        return "%s (%s) \u00b7 %s" % (label, scope, acct)
    return "%s \u00b7 %s stream %s" % (label, acct, rule.get("stream"))


def _same_rule(a, b) -> bool:
    fields = ("kind", "key", "acct", "stream")
    return (all(a.get(f) == b.get(f) for f in fields)
            and _rule_season(a) == _rule_season(b))


def add_avoid_from_recent(play_number, season_only=False) -> dict:
    """Create an avoid rule for recent play #*play_number*.

    -> {"ok": bool, "message": str, "rule": dict|None}
    Episodes avoid that PROVIDER for the show (or just that season with
    *season_only*); movies avoid that exact copy. A saved pick that names the
    same provider/copy would outrank the new rule, so it is removed (whole-show
    and movie rules only; a season rule leaves the show-wide pick and says so).
    """
    try:
        n = int(float(play_number))
    except (TypeError, ValueError):
        n = 0
    plays = get_recent_plays()
    if not plays:
        return {"ok": False, "rule": None,
                "message": "No recent plays recorded yet -- play the title first."}
    if not 1 <= n <= len(plays):
        return {"ok": False, "rule": None,
                "message": "Play # must be 1-%d (see 'List recent plays')." % len(plays)}
    entry = plays[n - 1]
    obj, label = _resolve_play(entry)
    if obj is None:
        return {"ok": False, "rule": None, "message": "Play #%d: %s." % (n, label)}
    acct = entry.get("acct")
    acct_name = _account_names({acct}).get(acct) or "account %s" % acct
    if entry.get("type") == "episode":
        key = _series_key(obj)
        if key is None:
            return {"ok": False, "rule": None, "message": "Play #%d: show has no usable id." % n}
        season = None
        if season_only:
            try:
                season = int(obj.season_number)
            except (TypeError, ValueError):
                season = None
        series_name = getattr(getattr(obj, "series", None), "name", None) or label
        rule = {"kind": "episode", "key": key, "acct": acct, "stream": None,
                "season": season, "label": series_name, "acct_name": acct_name,
                "ts": time.time()}
    else:
        fake_rel = types.SimpleNamespace(m3u_account_id=acct, stream_id=entry.get("stream"))
        rule = {"kind": "movie", "key": _capture_key(obj, fake_rel), "acct": acct,
                "stream": str(entry.get("stream")), "season": None, "label": label,
                "acct_name": acct_name, "ts": time.time()}

    state = {"dup": False, "full": False}

    def _mutate(rules):
        if any(_same_rule(r, rule) for r in rules):
            state["dup"] = True
            return False
        if len(rules) >= MAX_AVOID_RULES:
            state["full"] = True
            return False
        rules.append(rule)
        return True

    _write_avoid(_mutate)
    if state["dup"]:
        return {"ok": True, "rule": rule, "message": "Already avoiding: %s." % _rule_text(rule)}
    if state["full"]:
        return {"ok": False, "rule": None,
                "message": "Avoid-rule limit (%d) reached; remove some first." % MAX_AVOID_RULES}

    note = ""
    pick = (get_saved_picks() or {}).get(rule["key"])
    if isinstance(pick, dict) and pick.get("m3u_account_id") == acct:
        same_copy = rule["kind"] == "episode" or str(pick.get("stream_id")) == rule["stream"]
        if same_copy and rule.get("season") is None:
            clear_saved_key(rule["key"])
            note = " Removed the saved pick that chose this copy."
        elif same_copy:
            note = (" Note: a saved pick for the whole show chooses this provider and "
                    "outranks this rule; clear '%s' to let it apply." % rule["key"])
    logger.info("[VOD-PREF] avoid rule added: %s", _rule_text(rule))
    return {"ok": True, "rule": rule, "message": "Now avoiding: %s.%s" % (_rule_text(rule), note)}


def get_avoid_rules() -> list:
    """The avoid rules straight from the DB (uncached), oldest first."""
    try:
        return _read_avoid_from_db()
    except Exception as exc:
        logger.error("[VOD-PREF] could not read avoid rules: %s", exc)
        return []


def format_avoid_rules(rules) -> str:
    """Numbered (stable: #1 = oldest), newest shown first, capped for the popup."""
    if not rules:
        return "No avoid rules."
    numbered = list(enumerate(rules, 1))[::-1]
    shown = numbered[:AVOID_LIST_SHOWN]
    parts = ["#%d %s" % (n, _rule_text(r)) for n, r in shown]
    head = "%d avoid rule(s)" % len(rules)
    if len(rules) > len(shown):
        head += ", newest %d shown" % len(shown)
    return head + ": " + "  |  ".join(parts)


def remove_avoid_rule(rule_number) -> dict:
    """Remove avoid rule #*rule_number* (as numbered by the List action)."""
    try:
        n = int(float(rule_number))
    except (TypeError, ValueError):
        n = 0
    removed = {}

    def _mutate(rules):
        if 1 <= n <= len(rules):
            removed["rule"] = rules.pop(n - 1)
            return True
        removed["count"] = len(rules)
        return False

    _write_avoid(_mutate)
    if "rule" in removed:
        return {"ok": True, "message": "Removed avoid rule #%d: %s. Later rules move up "
                                       "one number." % (n, _rule_text(removed["rule"]))}
    return {"ok": False, "message": "No avoid rule #%s (there are %d)." % (
        rule_number, removed.get("count", 0))}


def clear_all_avoid() -> int:
    """Remove every avoid rule. Returns how many were removed."""
    removed = {"n": 0}

    def _mutate(rules):
        removed["n"] = len(rules)
        if not rules:
            return False
        del rules[:]
        return True

    _write_avoid(_mutate)
    logger.info("[VOD-PREF] cleared %d avoid rule(s)", removed["n"])
    return removed["n"]
