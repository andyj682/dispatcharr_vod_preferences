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
     else is available.
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
workers, enable + restart so every worker imports and patches; confirm from the
per-worker `[VOD-PREF] active ... pid=` log lines.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger("plugins.dispatcharr_vod_preferences")

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
      4. the provider/ACCOUNT name (relation.m3u_account.name) -- catches the
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

    # 4. Provider / account name.
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
        "saved_picks": picks if isinstance(picks, dict) else {},
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


def _quality_ranked(candidates, prefer, use_audio=False, avoid_dv=False):
    """Stable-sort candidates (best first) by a composite key.

    Key = (compatible, quality_rank, audio_rank), all higher-is-better:
      * compatible -- when *avoid_dv* is on, a Dolby-Vision-without-fallback
        stream scores 0 and everything else 1, so a PLAYABLE stream outranks a
        no-fallback DV stream even of higher resolution. Off -> all 1 (no effect).
      * quality_rank -- the prefer_quality ladder (0 when prefer is off/unset).
      * audio_rank -- SECONDARY tiebreak within a video tier, only when
        *use_audio* and a quality preference are both active.
    The sort is stable, so streams equal on every active key keep native order.
    """
    quality_on = bool(prefer) and prefer != "off"
    priority = _QUALITY_PRIORITY.get(prefer, _QUALITY_PRIORITY["4k"]) if quality_on else None
    return sorted(
        candidates,
        key=lambda c: (
            0 if (avoid_dv and _is_dv_no_fallback(c)) else 1,
            quality_rank(c, priority) if priority else 0,
            audio_rank(c) if (quality_on and use_audio) else 0,
        ),
        reverse=True,
    )


def _finalize_show_pick(chosen, candidates, prefer, use_audio=False, avoid_dv=False):
    """A TV show pick names a PROVIDER (account). Keep that provider, but still
    honour the quality rule and DV-avoidance WITHIN it -- so when one provider
    carries both the 4K and non-4K copy of an episode, Prefer 4K picks the 4K
    stream, and a compatible copy is preferred over a no-fallback DV one. Fail
    over to other providers (native order) only if the chosen provider can't serve it.
    """
    acct = chosen.m3u_account_id
    same = [c for c in candidates if c.m3u_account_id == acct]
    other = [c for c in candidates if c.m3u_account_id != acct]
    if same and ((prefer and prefer != "off") or avoid_dv):
        same = _quality_ranked(same, prefer, use_audio, avoid_dv)
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

    # 2. Saved UI pick for this title. Movies pin the exact stream; TV pins the
    #    provider for the whole series and still honours the quality rule within it.
    if remember:
        chosen = _lookup_saved(content_obj, candidates, cfg["saved_picks"])
        if chosen is not None:
            if _is_episode(content_obj):
                primary, ordered = _finalize_show_pick(chosen, candidates, prefer, use_audio, avoid_dv)
            else:
                primary, ordered = chosen, _front(candidates, chosen)
            return primary, ordered, "saved-pick"

    # 3. Quality rule and/or Dolby-Vision avoidance. Both re-order candidates;
    #    DV-avoidance (when on) is the TOP sort key, so a playable stream beats a
    #    no-fallback DV one even at higher resolution, while the quality rule and
    #    audio tiebreak order the rest. Audio can also decide when there's no
    #    video signal but an audio one is present; otherwise a no-signal quality
    #    result (and a DV-avoidance that changed nothing) falls through to native.
    quality_on = bool(prefer) and prefer != "off"
    if quality_on or avoid_dv:
        priority = _QUALITY_PRIORITY.get(prefer, _QUALITY_PRIORITY["4k"]) if quality_on else None
        ranked = _quality_ranked(candidates, prefer, use_audio, avoid_dv)
        top = ranked[0] if ranked else None
        if top is not None:
            has_quality = quality_on and quality_rank(top, priority) > 0
            has_audio = quality_on and use_audio and audio_rank(top) > 0
            avoided = avoid_dv and _is_dv_no_fallback(relation) and not _is_dv_no_fallback(top)
            if has_quality or has_audio:
                return top, ranked, "quality:%s" % prefer
            if avoided:
                return top, ranked, "avoid-dv"
        if quality_on:
            return relation, candidates, "quality:%s:no-signal" % prefer

    # 4. Native.
    return relation, candidates, "native"


# --------------------------------------------------------------------------- #
# Patched: _get_content_and_relation
# --------------------------------------------------------------------------- #

def patched_get_content_and_relation(content_type, content_id,
                                     preferred_m3u_account_id=None,
                                     preferred_stream_id=None):
    if not _ACTIVE:
        return _orig_get_content_and_relation(
            content_type, content_id, preferred_m3u_account_id, preferred_stream_id
        )

    _log_pid_once("select")

    content_obj, relation, candidates = _orig_get_content_and_relation(
        content_type, content_id, preferred_m3u_account_id, preferred_stream_id
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
                "dv_nofallback=%s, changed=%s, candidates=%d)",
                content_type, _log_label(content_obj, new_relation), reason,
                new_relation.m3u_account_id, new_relation.stream_id,
                quality_tier(new_relation), _audio_label(new_relation),
                _is_dv_no_fallback(new_relation), changed, len(new_candidates),
            )
        return content_obj, new_relation, new_candidates
    except Exception as exc:
        logger.error("[VOD-PREF] preference logic error, using native selection: %s", exc)
        return content_obj, relation, candidates


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
