"""
Self-contained logic test for the VOD preferences patch.

Runs WITHOUT Dispatcharr, Django, or a real database. It:
  * injects a fake `apps.plugins.models.PluginConfig` backed by a single
    in-memory settings dict, and a fake `django.db.transaction.atomic`, so the
    lazily-imported persistence bits of patch.py resolve,
  * stubs the native `_get_content_and_relation` with a function that reproduces
    Dispatcharr's real selection (honour an explicit stream_id, else the
    priority-ordered candidates[0]),
  * builds fake relations/content objects matching the real model attributes.

Checks the full selection ladder and the supporting helpers:
  * quality_tier / quality_rank read the per-stream custom_properties signal.
  * Quality rule re-sorts candidates, is a STABLE tiebreak among unknowns, and
    is a no-op when nothing carries a quality signal.
  * Prefer-1080p vs prefer-4K order differently.
  * An explicit request pick is passed through AND remembered (once, even for a
    replayed burst), keyed by tmdb/imdb/episode/stream as appropriate.
  * A saved pick is applied on a later (client playback) request, moved to the front for
    failover, and silently ignored when the provider dropped that stream.
  * remember_ui_picks=off disables both capture and application.
  * The ladder precedence (request > saved > quality > native) holds.
  * clear_all_saved / clear_saved_key mutate the store correctly.
  * The wrapper falls back to native selection if the ladder raises.
  * 1.4.0: video bitrate reading/normalization, the low-bitrate rule and its
    placement, category quality tags (incl. untagged), the Status registry.

Run:  python test_logic.py     (or: py -3 test_logic.py)
"""

import contextlib
import importlib.util
import os
import sys
import types

_here = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Fake DB: a single PluginConfig row whose settings dict lives in DB["settings"]
# --------------------------------------------------------------------------- #
DB = {"settings": {}}

PLUGIN_KEY = "dispatcharr_vod_preferences"


class _FakeObj:
    """Stand-in for a PluginConfig row returned by .first()."""

    def __init__(self):
        self.settings = dict(DB["settings"])

    def save(self, update_fields=None):
        DB["settings"] = dict(self.settings)


class _FakeQS:
    def __init__(self, exists):
        self.exists = exists
        self._values = False

    def values(self, *fields):
        self._values = True
        return self

    def select_for_update(self):
        return self

    def first(self):
        if not self.exists:
            return None
        if self._values:
            return {"settings": dict(DB["settings"])}
        return _FakeObj()


class _FakeManager:
    def filter(self, key=None, **kw):
        return _FakeQS(key == PLUGIN_KEY)

    def select_for_update(self):
        # Allow PluginConfig.objects.select_for_update().filter(...)
        return _ManagerSFU()


class _ManagerSFU:
    def filter(self, key=None, **kw):
        return _FakeQS(key == PLUGIN_KEY)


class PluginConfig:
    objects = _FakeManager()


# --------------------------------------------------------------------------- #
# Fake CoreSettings: a single row holding the picks map in CORE["value"]
# --------------------------------------------------------------------------- #
CORE = {"exists": False, "value": {}}
# A second row: the avoid rules (keyed by AVOID_CORE_KEY). Every other key maps
# to CORE (the picks row), as before.
AVOID = {"exists": False, "value": {}}
AVOID_KEY = "dispatcharr_vod_preferences_avoid"
# A third row: the played-shows record.
PLAYED = {"exists": False, "value": {}, "writes": 0}
PLAYED_KEY = "dispatcharr_vod_preferences_played"


def _store(key):
    if key == AVOID_KEY:
        return AVOID
    if key == PLAYED_KEY:
        return PLAYED
    return CORE


class _CoreRow:
    def __init__(self, store):
        self._store = store
        self.value = dict(store["value"])

    def save(self, update_fields=None):
        self._store["value"] = dict(self.value)
        self._store["exists"] = True
        self._store["writes"] = self._store.get("writes", 0) + 1


class _CoreQS:
    def __init__(self, store):
        self._store = store
        self._values = False

    def values(self, *fields):
        self._values = True
        return self

    def select_for_update(self):
        return self

    def first(self):
        if not self._store["exists"]:
            return None
        if self._values:
            return {"value": dict(self._store["value"])}
        return _CoreRow(self._store)


class _CoreManager:
    def filter(self, key=None, **kw):
        return _CoreQS(_store(key))

    def select_for_update(self):
        return _CoreManagerSFU()

    def create(self, key=None, name=None, value=None):
        st = _store(key)
        st["exists"] = True
        st["value"] = dict(value or {})
        st["writes"] = st.get("writes", 0) + 1
        return _CoreRow(st)


class _CoreManagerSFU:
    def filter(self, key=None, **kw):
        return _CoreQS(_store(key))


class CoreSettings:
    objects = _CoreManager()


def _install_fake_modules():
    apps_mod = types.ModuleType("apps")
    plugins_mod = types.ModuleType("apps.plugins")
    models_mod = types.ModuleType("apps.plugins.models")
    models_mod.PluginConfig = PluginConfig
    sys.modules["apps"] = apps_mod
    sys.modules["apps.plugins"] = plugins_mod
    sys.modules["apps.plugins.models"] = models_mod

    core_mod = types.ModuleType("core")
    core_models_mod = types.ModuleType("core.models")
    core_models_mod.CoreSettings = CoreSettings
    sys.modules["core"] = core_mod
    sys.modules["core.models"] = core_models_mod

    django_mod = sys.modules.get("django") or types.ModuleType("django")
    db_mod = types.ModuleType("django.db")

    @contextlib.contextmanager
    def _atomic(*a, **k):
        yield

    db_mod.transaction = types.SimpleNamespace(atomic=_atomic)
    sys.modules["django"] = django_mod
    sys.modules["django.db"] = db_mod


_install_fake_modules()

spec = importlib.util.spec_from_file_location("vodpref_patch", os.path.join(_here, "patch.py"))
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


# --------------------------------------------------------------------------- #
# Fake Dispatcharr model objects
# --------------------------------------------------------------------------- #
class FakeAccount:
    def __init__(self, name):
        self.name = name


class FakeRelation:
    def __init__(self, rid, account_id, stream_id, quality=None, resolution=None,
                 name=None, info_title=None, video=None, account_name=None,
                 audio=None):
        self.id = rid
        self.m3u_account_id = account_id
        self.stream_id = stream_id
        props = {}
        if quality is not None:
            props["quality"] = quality
        if resolution is not None:
            props["resolution"] = resolution
        if name is not None:  # provider stream name (movie basic_data.name)
            props["basic_data"] = {"name": name}
        if info_title is not None:  # provider episode title (info.title)
            props["info"] = {"title": info_title}
        if video is not None:  # detailed_info.video dims
            props.setdefault("detailed_info", {})["video"] = video
        if audio is not None:  # detailed_info.audio (ffprobe-style dict)
            props.setdefault("detailed_info", {})["audio"] = audio
        self.custom_properties = props
        # Only set m3u_account when a name is given, so relations that shouldn't
        # exercise the account-name rung simply don't have the attribute.
        if account_name is not None:
            self.m3u_account = FakeAccount(account_name)


class FakeSeries:
    def __init__(self, uuid, tmdb_id=None, imdb_id=None):
        self.uuid = uuid
        self.tmdb_id = tmdb_id
        self.imdb_id = imdb_id


class FakeMovie:
    def __init__(self, tmdb_id=None, imdb_id=None):
        self.tmdb_id = tmdb_id
        self.imdb_id = imdb_id


class FakeEpisode:
    def __init__(self, tmdb_id=None, imdb_id=None, series_uuid=None,
                 season_number=None, episode_number=None,
                 series_tmdb=None, series_imdb=None):
        self.tmdb_id = tmdb_id
        self.imdb_id = imdb_id
        self.series = (
            FakeSeries(series_uuid, tmdb_id=series_tmdb, imdb_id=series_imdb)
            if series_uuid else None
        )
        self.season_number = season_number
        self.episode_number = episode_number


def AUD(codec, channels=None, layout=None):
    """Build an ffprobe-style audio dict for tests."""
    d = {"codec_name": codec}
    if channels is not None:
        d["channels"] = channels
    if layout is not None:
        d["channel_layout"] = layout
    return d


def DV(width=3840, height=2160, compat=0, profile=5):
    """Build a video dict carrying a Dolby Vision DOVI configuration record."""
    rec = {"side_data_type": "DOVI configuration record", "dv_profile": profile}
    if compat is not None:
        rec["dv_bl_signal_compatibility_id"] = compat
    return {"width": width, "height": height, "side_data_list": [rec]}


def VID(width, height):
    """Build a plain (non-DV) video dict of the given dimensions."""
    return {"width": width, "height": height}


# --------------------------------------------------------------------------- #
# Native stub for _get_content_and_relation
# --------------------------------------------------------------------------- #
CURRENT = {"content": None, "candidates": []}

# Records every call the wrapper forwards to the original -- used by the
# signature-parity test to prove unknown args are ACCEPTED and FORWARDED, not
# just swallowed. The *args/**kwargs mirror upstream possibly gaining a parameter
# (positionally or as a keyword) on a future release.
NATIVE_CALLS = []


def native_stub(content_type, content_id, preferred_m3u_account_id=None,
                preferred_stream_id=None, *args, **kwargs):
    NATIVE_CALLS.append({"args": args, "kwargs": kwargs})
    content = CURRENT["content"]
    candidates = CURRENT["candidates"]
    relation = candidates[0] if candidates else None
    if preferred_stream_id:
        rel = next((r for r in candidates if str(r.stream_id) == str(preferred_stream_id)), None)
        if rel is not None:
            relation = rel
    elif preferred_m3u_account_id:
        rel = next((r for r in candidates if r.m3u_account_id == preferred_m3u_account_id), None)
        if rel is not None:
            relation = rel
    return content, relation, candidates


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def reset(fields=None, picks=None, legacy_picks=None, avoid=None):
    """Set up DB state.

    fields       -> PluginConfig.settings (prefer_quality / remember_ui_picks)
    picks        -> the CoreSettings picks row (None = row absent)
    legacy_picks -> pre-0.1.1 picks stashed in PluginConfig.settings (migration)
    """
    DB["settings"] = dict(fields or {})
    if legacy_picks is not None:
        DB["settings"]["saved_picks"] = dict(legacy_picks)
    if picks is None:
        CORE["exists"] = False
        CORE["value"] = {}
    else:
        CORE["exists"] = True
        CORE["value"] = dict(picks)
    AVOID["exists"] = bool(avoid)
    AVOID["value"] = {"rules": [dict(r) for r in avoid]} if avoid else {}
    patch._notified_local.clear()
    patch._last_play_local.clear()
    PLAYED.update(exists=False, value={}, writes=0)
    patch.invalidate_config_cache()
    patch._cat_tags_cache = None
    patch._orig_get_content_and_relation = native_stub
    patch._ACTIVE = True


def scenario(content, candidates):
    CURRENT["content"] = content
    CURRENT["candidates"] = candidates


def call(preferred_stream_id=None, preferred_m3u_account_id=None):
    return patch.patched_get_content_and_relation(
        "movie", "uuid-x", preferred_m3u_account_id, preferred_stream_id
    )


PASS = "PASS"
FAIL = "FAIL"
_failures = []


def check(name, cond):
    print(f"  [{PASS if cond else FAIL}] {name}")
    if not cond:
        _failures.append(name)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def test_quality_helpers():
    print("test_quality_helpers")
    check("quality '4K' -> 4k", patch.quality_tier(FakeRelation(1, 1, "a", quality="4K")) == "4k")
    check("quality '2160p' -> 4k", patch.quality_tier(FakeRelation(1, 1, "a", quality="2160p")) == "4k")
    check("quality 'UHD' -> 4k (not 720p 'hd')", patch.quality_tier(FakeRelation(1, 1, "a", quality="UHD")) == "4k")
    check("quality 'FHD' -> 1080p", patch.quality_tier(FakeRelation(1, 1, "a", quality="FHD")) == "1080p")
    check("resolution '1920x1080' -> 1080p", patch.quality_tier(FakeRelation(1, 1, "a", resolution="1920x1080")) == "1080p")
    check("resolution '1280x720' -> 720p", patch.quality_tier(FakeRelation(1, 1, "a", resolution="1280x720")) == "720p")
    check("quality 'HD' -> 720p", patch.quality_tier(FakeRelation(1, 1, "a", quality="HD")) == "720p")
    check("quality 'SD' -> 480p", patch.quality_tier(FakeRelation(1, 1, "a", quality="SD")) == "480p")
    check("no props -> None", patch.quality_tier(FakeRelation(1, 1, "a")) is None)
    check("unknown label -> None", patch.quality_tier(FakeRelation(1, 1, "a", quality="Potato")) is None)


def test_quality_signal_sources():
    print("test_quality_signal_sources")
    # provider stream name (movies -> basic_data.name)
    check("movie stream name '... 4K' -> 4k",
          patch.quality_tier(FakeRelation(1, 1, "a", name="EN - Example Movie (2002) 4K")) == "4k")
    check("movie stream name without a token -> None",
          patch.quality_tier(FakeRelation(1, 1, "a", name="EN - Example Movie (2002)")) is None)
    # episode provider title (info.title)
    check("episode title '2160p' -> 4k",
          patch.quality_tier(FakeRelation(1, 1, "a", info_title="Show S01E01 2160p")) == "4k")
    # account name -- the only 4K signal for episodes whose titles lack it
    check("account name 'Provider A 4K' -> 4k",
          patch.quality_tier(FakeRelation(1, 1, "a", info_title="Chapter One", account_name="Provider A 4K")) == "4k")
    check("account name 'Provider A' (no token) -> None",
          patch.quality_tier(FakeRelation(1, 1, "a", account_name="Provider A")) is None)
    # video dimensions (present after an advanced refresh)
    check("video 3840x2160 -> 4k",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 3840, "height": 2160})) == "4k")
    check("video 1280x720 -> 720p",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 1280, "height": 720})) == "720p")
    check("resolution text '3840x2160' -> 4k",
          patch.quality_tier(FakeRelation(1, 1, "a", resolution="3840x2160")) == "4k")
    # word-boundary guards: these must NOT be read as quality
    check("'Wednesday' is NOT SD",
          patch.quality_tier(FakeRelation(1, 1, "a", name="Wednesday (2022)")) is None)
    check("'24K Gold Rush' is NOT 4K",
          patch.quality_tier(FakeRelation(1, 1, "a", name="24K Gold Rush")) is None)
    check("year '1720' is NOT 720p",
          patch.quality_tier(FakeRelation(1, 1, "a", name="Something 1720")) is None)
    # precedence within the waterfall
    check("explicit field beats name",
          patch.quality_tier(FakeRelation(1, 1, "a", quality="1080p", name="Movie 4K")) == "1080p")
    check("per-stream name beats account name",
          patch.quality_tier(FakeRelation(1, 1, "a", name="Movie 1080p", account_name="Prov 4K")) == "1080p")


def test_cover_image_dims_ignored():
    print("test_cover_image_dims_ignored (real cover-image 4K case)")
    # A genuinely-4K episode whose only info.info.video is an attached 1920x1080
    # PNG poster must NOT be read as 1080p -- fall through to the '4K' title.
    r = FakeRelation(1, 7, "s4k", info_title="4K - Example Show (2025) - S01E01 - Pilot")
    r.custom_properties["info"]["info"] = {"video": {
        "width": 1920, "height": 1080, "codec_name": "png",
        "disposition": {"attached_pic": 1}, "tags": {"mimetype": "image/png"},
    }}
    check("cover-image PNG dims ignored -> 4K from title", patch.quality_tier(r) == "4k")

    # A REAL video track's dimensions are still honoured over a plain title.
    r2 = FakeRelation(2, 7, "s2", info_title="EN - Example Show (2025) - S01E01")
    r2.custom_properties["info"]["info"] = {"video": {
        "width": 3840, "height": 1606, "codec_name": "hevc",
        "disposition": {"attached_pic": 0, "default": 1},
    }}
    check("real 3840-wide video -> 4k", patch.quality_tier(r2) == "4k")

    r3 = FakeRelation(3, 7, "s3", info_title="EN - Example Show (2025) - S01E01")
    r3.custom_properties["info"]["info"] = {"video": {
        "width": 1920, "height": 1080, "codec_name": "h264",
        "disposition": {"attached_pic": 0, "default": 1},
    }}
    check("real 1920-wide video -> 1080p", patch.quality_tier(r3) == "1080p")

    # Real dimensions are the TOP signal: they override a conflicting title,
    # so a fake-4K label on a genuinely 1080p track ranks as 1080p.
    r4 = FakeRelation(4, 7, "s4", info_title="Fake 4K Upscale S01E01")
    r4.custom_properties["info"]["info"] = {"video": {
        "width": 1920, "height": 1080, "codec_name": "h264",
        "disposition": {"attached_pic": 0, "default": 1},
    }}
    check("real 1080p dims override a '4K' title", patch.quality_tier(r4) == "1080p")


def test_account_name_ranks_episodes():
    print("test_account_name_ranks_episodes")
    # Real-world shape: an episode carried by a 4K-named account and a plain one,
    # with no per-stream quality in the titles. prefer-4k must pick the 4K account.
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    r_hd = FakeRelation(60, 1, "s_hd", info_title="Chapter One", account_name="Provider A")
    r_4k = FakeRelation(61, 2, "s_4k", info_title="Chapter One", account_name="Provider A 4K")
    ep = FakeEpisode(tmdb_id="700", season_number=1, episode_number=1)
    CURRENT["content"] = ep
    CURRENT["candidates"] = [r_hd, r_4k]  # native order: plain account first
    _, rel, cands = patch.patched_get_content_and_relation("episode", "uuid-e", None, None)
    check("prefer-4k picks the 4K-account stream", rel.id == r_4k.id)
    check("4K account ahead in failover", [c.id for c in cands] == [r_4k.id, r_hd.id])


def test_multi_relation_episode_real_shape():
    print("test_multi_relation_episode_real_shape (5 relations, 4K marked in info.title)")
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    # Real-world shape observed on an instance: one episode with five relations
    # across two providers; the two 4K copies carry '4K' in the provider title.
    r_b4k = FakeRelation(1, 10, "b4k",
                         info_title="4K - Example Show (2022) (US) - S01E01 - Pilot",
                         account_name="Provider B 4K")
    r_a = FakeRelation(2, 11, "a1",
                       info_title="EN - Example Show (2022) - S01E01 - Pilot",
                       account_name="Provider A")
    r_a4k = FakeRelation(3, 12, "a4k",
                         info_title="EN - Example Show (2022) 4K - S01E01 - Pilot",
                         account_name="Provider A 4K")
    r_b1 = FakeRelation(4, 13, "b1",
                        info_title="EN - Example Show (2022) - S01E01 - Pilot",
                        account_name="Provider B")
    r_b2 = FakeRelation(5, 13, "b2",
                        info_title="EN - Example Show - S01E01",
                        account_name="Provider B")
    CURRENT["content"] = FakeEpisode(season_number=1, episode_number=1)
    CURRENT["candidates"] = [r_b4k, r_a, r_a4k, r_b1, r_b2]  # native priority order
    _, rel, cands = patch.patched_get_content_and_relation("episode", "uuid-ep", None, None)
    check("both 4K relations recognised via info.title",
          patch.quality_tier(r_b4k) == "4k" and patch.quality_tier(r_a4k) == "4k")
    check("non-4K relations recognised as unknown",
          all(patch.quality_tier(r) is None for r in (r_a, r_b1, r_b2)))
    check("prefer-4k picks a 4K relation (highest native among 4K)", rel.id == r_b4k.id)
    check("both 4K first, then non-4K in native order",
          [c.id for c in cands] == [r_b4k.id, r_a4k.id, r_a.id, r_b1.id, r_b2.id])


def test_quality_rule_reorders():
    print("test_quality_rule_reorders")
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    # native order (account priority): 720p, 4K, 1080p
    r720 = FakeRelation(10, 1, "s720", quality="720p")
    r4k = FakeRelation(11, 2, "s4k", quality="4K")
    r1080 = FakeRelation(12, 3, "s1080", quality="1080p")
    scenario(FakeMovie(tmdb_id="100"), [r720, r4k, r1080])
    _, rel, cands = call()
    check("prefer-4k picks the 4K stream", rel.id == r4k.id)
    check("failover order is 4K,1080p,720p", [c.id for c in cands] == [r4k.id, r1080.id, r720.id])


def test_prefer_1080p_differs():
    print("test_prefer_1080p_differs")
    reset(fields={"prefer_quality": "1080p", "remember_ui_picks": False})
    # native order: 4K, 1080p, 720p
    r4k = FakeRelation(11, 2, "s4k", quality="4K")
    r1080 = FakeRelation(12, 3, "s1080", quality="1080p")
    r720 = FakeRelation(13, 4, "s720", quality="720p")
    scenario(FakeMovie(tmdb_id="100"), [r4k, r1080, r720])
    _, rel, cands = call()
    check("prefer-1080p picks the 1080p stream", rel.id == r1080.id)
    check("prefer-1080p ranks 4K LAST (1080p,720p,4K)",
          [c.id for c in cands] == [r1080.id, r720.id, r4k.id])


def test_prefer_720p_order():
    print("test_prefer_720p_order")
    reset(fields={"prefer_quality": "720p", "remember_ui_picks": False})
    # native order: 4K, 1080p, 720p, 480p
    r4k = FakeRelation(30, 1, "s4k", quality="4K")
    r1080 = FakeRelation(31, 2, "s1080", quality="1080p")
    r720 = FakeRelation(32, 3, "s720", quality="720p")
    r480 = FakeRelation(33, 4, "s480", quality="480p")
    scenario(FakeMovie(tmdb_id="100"), [r4k, r1080, r720, r480])
    _, rel, cands = call()
    check("prefer-720p picks the 720p stream", rel.id == r720.id)
    check("prefer-720p order: 720p,480p,1080p,4K (step down before up, 4K last)",
          [c.id for c in cands] == [r720.id, r480.id, r1080.id, r4k.id])

    # 720p absent -> steps DOWN to 480p before UP to 1080p, 4K still last.
    scenario(FakeMovie(tmdb_id="100"), [r4k, r1080, r480])
    _, rel, cands = call()
    check("prefer-720p with no 720p falls to 480p first", rel.id == r480.id)
    check("prefer-720p failover 480p,1080p,4K",
          [c.id for c in cands] == [r480.id, r1080.id, r4k.id])


def test_dims_cinemascope_tolerance():
    print("test_dims_cinemascope_tolerance")
    # A cropped 2.39:1 1080p master is often a few px under 1920 wide. It must
    # still classify as 1080p, not fall through to 720p on a strict < 1920 cut.
    check("1918x800 cinemascope -> 1080p",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 1918, "height": 800})) == "1080p")
    check("1912x800 cinemascope -> 1080p",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 1912, "height": 800})) == "1080p")
    # 4K cinemascope a few px under 3840 stays 4K.
    check("3836x1600 4K scope -> 4k",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 3836, "height": 1600})) == "4k")
    # Tolerance must NOT promote a genuine lower tier across the wide gap.
    check("1280x720 stays 720p (not promoted to 1080p)",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 1280, "height": 720})) == "720p")
    check("1600x900 stays 720p (below 1080p tolerance band)",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 1600, "height": 900})) == "720p")
    check("854x480 stays 480p (not promoted to 720p)",
          patch.quality_tier(FakeRelation(1, 1, "a", video={"width": 854, "height": 480})) == "480p")


def test_audio_rank():
    print("test_audio_rank")
    q = lambda **kw: patch.audio_rank(FakeRelation(1, 1, "a", **kw))
    # surround: lossless > dolby > aac > other
    r_sl = q(audio=AUD("truehd", 8, "7.1"))
    r_sd = q(audio=AUD("eac3", 6))
    r_sa = q(audio=AUD("aac", 6))
    r_so = q(audio=AUD("opus", 6))
    # stereo: lossless > dolby > aac > other
    r_tl = q(audio=AUD("dts", 2))
    r_td = q(audio=AUD("ac3", 2))
    r_ta = q(audio=AUD("aac", 2))
    r_to = q(audio=AUD("mp3", 2))
    check("surround lossless (TrueHD) is the top rank", r_sl == 8)
    check("full order descends lossless>dolby>aac>other, surround before stereo",
          r_sl > r_sd > r_sa > r_so > r_tl > r_td > r_ta > r_to)
    check("stereo/other still ranked above nothing", r_to == 1)
    check("DTS classified lossless (top of surround)", q(audio=AUD("dts", 6)) == 8)
    check("TrueHD classified lossless", q(audio=AUD("truehd", 6)) == 8)
    check("mono (other channels) -> 0", q(audio=AUD("truehd", 1)) == 0)
    check("no audio info -> 0", patch.audio_rank(FakeRelation(1, 1, "a")) == 0)
    check("channel_layout drives surround when channels missing",
          q(audio=AUD("eac3", None, "5.1(side)")) == 7)
    check("surround-first: lossy MP3 5.1 still outranks lossless DTS 2.0",
          q(audio=AUD("mp3", 6)) > q(audio=AUD("dts", 2)))


def test_audio_tiebreak_within_tier():
    print("test_audio_tiebreak_within_tier")
    r_aac = FakeRelation(1, 1, "aac2", video={"width": 1920, "height": 1080}, audio=AUD("aac", 2))
    r_ac3 = FakeRelation(2, 2, "ac3_51", video={"width": 1920, "height": 1080}, audio=AUD("eac3", 6))

    # prefer_audio ON: 5.1 promoted ahead of AAC 2.0 within the same 1080p tier.
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "prefer_audio": True})
    scenario(FakeMovie(tmdb_id="1"), [r_aac, r_ac3])  # native: aac first
    _, rel, cands = call()
    check("audio tiebreak promotes 5.1 within same video tier", rel.id == r_ac3.id)
    check("failover follows audio order", [c.id for c in cands] == [r_ac3.id, r_aac.id])

    # prefer_audio OFF: native order within the tier is preserved.
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "prefer_audio": False})
    scenario(FakeMovie(tmdb_id="1"), [r_aac, r_ac3])
    _, rel, _ = call()
    check("prefer_audio off -> native order kept within tier", rel.id == r_aac.id)

    # Video tier still dominates: a 4K/AAC-2.0 beats a 1080p/EAC3-5.1.
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "prefer_audio": True})
    r_4k = FakeRelation(3, 3, "4kaac", video={"width": 3840, "height": 2160}, audio=AUD("aac", 2))
    scenario(FakeMovie(tmdb_id="1"), [r_ac3, r_4k])  # native: 1080p eac3 first
    _, rel, _ = call()
    check("video tier dominates audio (4K AAC beats 1080p 5.1)", rel.id == r_4k.id)


def test_audio_decides_when_no_video_signal():
    print("test_audio_decides_when_no_video_signal")
    r_stereo = FakeRelation(1, 1, "s2", audio=AUD("aac", 2))
    r_surround = FakeRelation(2, 2, "s6", audio=AUD("ac3", 6))

    # Quality pref set, prefer_audio on, NO video signal anywhere: audio alone
    # drives the pick rather than falling through to native.
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "prefer_audio": True})
    scenario(FakeMovie(tmdb_id="1"), [r_stereo, r_surround])  # native: stereo first
    _, rel, _ = call()
    check("audio decides when video is all-unknown", rel.id == r_surround.id)

    # prefer_audio off + no video signal -> native (no reshuffle).
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "prefer_audio": False})
    scenario(FakeMovie(tmdb_id="1"), [r_stereo, r_surround])
    _, rel, _ = call()
    check("no video signal + audio off -> native", rel.id == r_stereo.id)


def test_dv_no_fallback_detection():
    print("test_dv_no_fallback_detection")
    d = lambda **kw: patch._is_dv_no_fallback(FakeRelation(1, 1, "a", **kw))
    check("DOVI compat=0 -> no fallback", d(video=DV(compat=0)) is True)
    check("DOVI compat=1 (HDR10 base) -> ok", d(video=DV(compat=1, profile=8)) is False)
    check("DOVI compat=2 (SDR base) -> ok", d(video=DV(compat=2, profile=8)) is False)
    check("DOVI compat=4 (HLG base) -> ok", d(video=DV(compat=4, profile=8)) is False)
    check("DOVI profile 5, compat missing -> no fallback",
          d(video=DV(compat=None, profile=5)) is True)
    check("plain 4K (no DOVI) -> not flagged", d(video=VID(3840, 2160)) is False)
    check("no video info -> not flagged", patch._is_dv_no_fallback(FakeRelation(1, 1, "a")) is False)


def test_avoid_dv_prefers_compatible():
    print("test_avoid_dv_prefers_compatible")
    dv = FakeRelation(1, 1, "dv", video=DV(compat=0))          # DV-P5 4K, top priority
    ok4k = FakeRelation(2, 2, "ok4k", video=VID(3840, 2160))   # compatible 4K
    ok1080 = FakeRelation(3, 2, "ok1080", video=VID(1920, 1080))  # compatible 1080p

    # avoid on + prefer 4k: a compatible 4K beats the no-fallback DV 4K.
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "avoid_dv_no_fallback": True})
    scenario(FakeMovie(tmdb_id="1"), [dv, ok4k])  # native: DV first
    _, rel, cands = call()
    check("compatible 4K chosen over no-fallback DV 4K", rel.id == ok4k.id)
    check("no-fallback DV demoted to last", [c.id for c in cands] == [ok4k.id, dv.id])

    # Only a compatible 1080p available: still beats the DV-P5 4K (compat is top key).
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "avoid_dv_no_fallback": True})
    scenario(FakeMovie(tmdb_id="1"), [dv, ok1080])
    _, rel, _ = call()
    check("compatible 1080p beats no-fallback DV 4K", rel.id == ok1080.id)

    # DV-P5 is the ONLY option: still served (never excluded).
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "avoid_dv_no_fallback": True})
    scenario(FakeMovie(tmdb_id="1"), [dv])
    _, rel, _ = call()
    check("no-fallback DV served when it's the only candidate", rel.id == dv.id)


def test_avoid_dv_off_keeps_dv():
    print("test_avoid_dv_off_keeps_dv")
    # Default off: prefer 4k still picks the DV-P5 4K (highest tier), unchanged.
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False, "avoid_dv_no_fallback": False})
    dv = FakeRelation(1, 1, "dv", video=DV(compat=0))
    ok1080 = FakeRelation(2, 2, "ok1080", video=VID(1920, 1080))
    scenario(FakeMovie(tmdb_id="1"), [dv, ok1080])
    _, rel, _ = call()
    check("avoid off -> DV-P5 4K still chosen by prefer 4k", rel.id == dv.id)


def test_avoid_dv_with_quality_off():
    print("test_avoid_dv_with_quality_off")
    dv = FakeRelation(1, 1, "dv", video=DV(compat=0))
    ok = FakeRelation(2, 2, "ok", video=VID(1920, 1080))

    # prefer off, avoid on: demote the DV-P5 native primary to a compatible one.
    reset(fields={"prefer_quality": "off", "remember_ui_picks": False, "avoid_dv_no_fallback": True})
    scenario(FakeMovie(tmdb_id="1"), [dv, ok])  # native primary = DV-P5
    _, rel, cands = call()
    check("prefer off + avoid on: compatible chosen over native DV-P5", rel.id == ok.id)
    check("DV-P5 moved last", [c.id for c in cands] == [ok.id, dv.id])

    # prefer off, avoid on, native primary already compatible -> native (no reshuffle).
    reset(fields={"prefer_quality": "off", "remember_ui_picks": False, "avoid_dv_no_fallback": True})
    scenario(FakeMovie(tmdb_id="1"), [ok, dv])
    _, rel, cands = call()
    check("primary already compatible -> unchanged", rel.id == ok.id)
    check("native order kept", [c.id for c in cands] == [ok.id, dv.id])


def test_quality_stable_for_unknowns():
    print("test_quality_stable_for_unknowns")
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    a = FakeRelation(20, 1, "a")  # unknown
    b = FakeRelation(21, 2, "b")  # unknown
    c = FakeRelation(22, 3, "c")  # unknown
    scenario(FakeMovie(tmdb_id="100"), [a, b, c])
    _, rel, cands = call()
    check("all-unknown -> native primary kept", rel.id == a.id)
    check("all-unknown -> native order kept (stable no-op)", [x.id for x in cands] == [a.id, b.id, c.id])

    # Mixed: one 4K among unknowns; unknowns keep their relative order behind it.
    d4k = FakeRelation(23, 4, "d", quality="4K")
    scenario(FakeMovie(tmdb_id="100"), [a, b, d4k, c])
    _, rel, cands = call()
    check("mixed -> 4K first", rel.id == d4k.id)
    check("mixed -> unknowns keep native relative order", [x.id for x in cands] == [d4k.id, a.id, b.id, c.id])


def test_capture_and_persist_once():
    print("test_capture_and_persist_once")
    reset(fields={"prefer_quality": "off", "remember_ui_picks": True})
    r1 = FakeRelation(30, 1, "s1")
    r2 = FakeRelation(31, 2, "s2")
    scenario(FakeMovie(tmdb_id="555"), [r1, r2])

    # UI plays stream s2 explicitly (three times, mimicking a client's open-file burst).
    for _ in range(3):
        _, rel, cands = call(preferred_stream_id="s2")
        check("explicit pick passed through", rel.id == r2.id)
    picks = patch.get_saved_picks()
    check("pick stored under tmdb key", picks.get("tmdb:555") == {"m3u_account_id": 2, "stream_id": "s2"})
    check("exactly one entry after burst", len(picks) == 1)


def test_episode_capture_is_show_level():
    print("test_episode_capture_is_show_level")
    # The Series UI sends m3u_account_id (no stream_id). Capture must fire and
    # store the PROVIDER for the whole series (account only, keyed by series id).
    reset(fields={"prefer_quality": "off", "remember_ui_picks": True})
    ep = FakeEpisode(series_uuid="ser-xyz", season_number=2, episode_number=5)
    r1 = FakeRelation(1, 7, "ep_s1")
    r2 = FakeRelation(2, 9, "ep_s2")
    CURRENT["content"] = ep
    CURRENT["candidates"] = [r1, r2]
    _, rel, _ = patch.patched_get_content_and_relation("episode", "uuid-e", 9, None)  # account 9, no stream_id
    check("account-id pick honoured -> chosen relation", rel.id == r2.id)
    picks = patch.get_saved_picks()
    check("stored as show-level provider (account only, series key)",
          picks.get("series:ser-xyz") == {"m3u_account_id": 9})
    check("not stored under a per-episode key", "ep:ser-xyz:S02E05" not in picks)


def test_show_level_pick_applies_across_episodes():
    print("test_show_level_pick_applies_across_episodes")
    # A provider remembered for the series must apply to a DIFFERENT episode,
    # matching by account (each episode has its own stream_id on that account).
    reset(
        fields={"prefer_quality": "off", "remember_ui_picks": True},
        picks={"series:ser-xyz": {"m3u_account_id": 9}},
    )
    # A different episode of the same series; native primary is account 7.
    ep2 = FakeEpisode(series_uuid="ser-xyz", season_number=3, episode_number=1)
    r_a = FakeRelation(10, 7, "e2_a")   # account 7 (native first)
    r_b = FakeRelation(11, 9, "e2_b")   # account 9 (the remembered provider)
    CURRENT["content"] = ep2
    CURRENT["candidates"] = [r_a, r_b]
    _, rel, cands = patch.patched_get_content_and_relation("episode", "uuid-e2", None, None)  # client play path
    check("show-level pick applies to another episode (by account)", rel.id == r_b.id)
    check("chosen provider moved to front for failover", [c.id for c in cands] == [r_b.id, r_a.id])

    # Series with tmdb: keyed on series tmdb, not uuid.
    reset(fields={"prefer_quality": "off", "remember_ui_picks": True})
    ep3 = FakeEpisode(series_uuid="ser-q", series_tmdb="88", season_number=1, episode_number=1)
    CURRENT["content"] = ep3
    CURRENT["candidates"] = [FakeRelation(20, 3, "x"), FakeRelation(21, 4, "y")]
    patch.patched_get_content_and_relation("episode", "uuid-e3", 4, None)
    check("series with tmdb keyed as stmdb:<id>",
          patch.get_saved_picks().get("stmdb:88") == {"m3u_account_id": 4})


def test_saved_pick_applied_on_emby_path():
    print("test_saved_pick_applied_on_emby_path")
    reset(
        fields={"prefer_quality": "off", "remember_ui_picks": True},
        picks={"tmdb:555": {"m3u_account_id": 2, "stream_id": "s2"}},
    )
    r1 = FakeRelation(30, 1, "s1")
    r2 = FakeRelation(31, 2, "s2")
    scenario(FakeMovie(tmdb_id="555"), [r1, r2])
    # Client play path: no preferred_stream_id; native would pick r1 (candidates[0]).
    _, rel, cands = call()
    check("saved pick overrides native primary", rel.id == r2.id)
    check("saved pick moved to front for failover", [c.id for c in cands] == [r2.id, r1.id])


def test_saved_pick_dropped_when_stream_gone():
    print("test_saved_pick_dropped_when_stream_gone")
    reset(
        fields={"prefer_quality": "off", "remember_ui_picks": True},
        picks={"tmdb:555": {"m3u_account_id": 9, "stream_id": "gone"}},
    )
    r1 = FakeRelation(30, 1, "s1")
    r2 = FakeRelation(31, 2, "s2")
    scenario(FakeMovie(tmdb_id="555"), [r1, r2])
    _, rel, cands = call()
    check("missing saved stream -> native primary", rel.id == r1.id)
    check("missing saved stream -> native order", [c.id for c in cands] == [r1.id, r2.id])


def test_stream_fallback_key():
    print("test_stream_fallback_key")
    # Movie with no tmdb/imdb: capture keys on the picked stream itself.
    reset(fields={"prefer_quality": "off", "remember_ui_picks": True})
    r1 = FakeRelation(40, 1, "s1")
    r2 = FakeRelation(41, 2, "s2")
    scenario(FakeMovie(), [r1, r2])
    call(preferred_stream_id="s2")
    picks = patch.get_saved_picks()
    check("fallback key is as:account:stream", "as:2:s2" in picks)

    # Later client-playback request: fallback pick applied by scanning candidates.
    patch.invalidate_config_cache()
    scenario(FakeMovie(), [r1, r2])
    _, rel, cands = call()
    check("fallback saved pick applied", rel.id == r2.id)


def test_show_pick_composes_with_quality_same_account():
    print("test_show_pick_composes_with_quality_same_account (the same-account 4K case)")
    # Consolidated world: 4K and non-4K of a series live on ONE account. A show
    # pick names that account; Prefer 4K then chooses the 4K stream WITHIN it.
    reset(
        fields={"prefer_quality": "4k", "remember_ui_picks": True},
        picks={"series:ser-c": {"m3u_account_id": 5}},
    )
    ep = FakeEpisode(series_uuid="ser-c", season_number=1, episode_number=1)
    hd = FakeRelation(1, 5, "e_hd", info_title="EN - Show (2022) - S01E01")       # account 5, no token
    fourk = FakeRelation(2, 5, "e_4k", info_title="EN - Show (2022) 4K - S01E01")  # account 5, 4K
    other = FakeRelation(3, 8, "e_other")                                          # different account
    CURRENT["content"] = ep
    CURRENT["candidates"] = [hd, fourk, other]   # native order: hd first
    _, rel, cands = patch.patched_get_content_and_relation("episode", "uuid-e", None, None)
    check("same-account: show pick + prefer-4k picks the 4K stream", rel.id == fourk.id)
    check("chosen provider's streams first (4K ahead of HD), other provider last",
          [c.id for c in cands] == [fourk.id, hd.id, other.id])

    # With prefer OFF, a same-account show pick can only honour the provider
    # (first stream in native order) -- it cannot distinguish quality alone.
    reset(
        fields={"prefer_quality": "off", "remember_ui_picks": True},
        picks={"series:ser-c": {"m3u_account_id": 5}},
    )
    CURRENT["content"] = ep
    CURRENT["candidates"] = [hd, fourk, other]
    _, rel, _ = patch.patched_get_content_and_relation("episode", "uuid-e", None, None)
    check("same-account, prefer off: falls to provider's native-first stream", rel.id == hd.id)


def test_remember_off_disables_feature():
    print("test_remember_off_disables_feature")
    # Capture disabled.
    reset(fields={"prefer_quality": "off", "remember_ui_picks": False})
    scenario(FakeMovie(tmdb_id="777"), [FakeRelation(60, 1, "s1"), FakeRelation(61, 2, "s2")])
    call(preferred_stream_id="s2")
    check("remember off -> nothing captured", patch.get_saved_picks() == {})

    # Application disabled even if a pick exists in the store.
    reset(
        fields={"prefer_quality": "off", "remember_ui_picks": False},
        picks={"tmdb:777": {"m3u_account_id": 2, "stream_id": "s2"}},
    )
    r1 = FakeRelation(60, 1, "s1")
    r2 = FakeRelation(61, 2, "s2")
    scenario(FakeMovie(tmdb_id="777"), [r1, r2])
    _, rel, _ = call()
    check("remember off -> saved pick ignored", rel.id == r1.id)


def test_ladder_precedence():
    print("test_ladder_precedence")
    # Saved pick beats the quality rule; explicit request beats the saved pick.
    reset(
        fields={"prefer_quality": "4k", "remember_ui_picks": True},
        picks={"tmdb:900": {"m3u_account_id": 1, "stream_id": "s1080"}},
    )
    r1080 = FakeRelation(70, 1, "s1080", quality="1080p")
    r4k = FakeRelation(71, 2, "s4k", quality="4K")
    scenario(FakeMovie(tmdb_id="900"), [r4k, r1080])  # native/quality would prefer 4K
    _, rel, _ = call()
    check("saved 1080p pick beats quality-4k rule", rel.id == r1080.id)

    scenario(FakeMovie(tmdb_id="900"), [r4k, r1080])
    _, rel, _ = call(preferred_stream_id="s4k")
    check("explicit request beats saved pick", rel.id == r4k.id)
    check("explicit request re-persists as new default",
          patch.get_saved_picks().get("tmdb:900") == {"m3u_account_id": 2, "stream_id": "s4k"})


def test_clear_operations():
    print("test_clear_operations")
    reset(
        fields={"remember_ui_picks": True},
        picks={
            "tmdb:1": {"m3u_account_id": 1, "stream_id": "a"},
            "imdb:tt2": {"m3u_account_id": 2, "stream_id": "b"},
            "as:3:c": {"m3u_account_id": 3, "stream_id": "c"},
        },
    )
    check("clear by bare tmdb id", patch.clear_saved_key("1") is True)
    check("tmdb:1 gone", "tmdb:1" not in patch.get_saved_picks())
    check("clear by exact key", patch.clear_saved_key("as:3:c") is True)
    check("clear non-existent -> False", patch.clear_saved_key("nope") is False)
    remaining = patch.clear_all_saved()
    check("clear_all reports remaining count", remaining == 1)
    check("store empty after clear_all", patch.get_saved_picks() == {})


def test_clear_all_actually_clears():
    print("test_clear_all_actually_clears")
    # Regression for the live bug: clear must persist so a subsequent read is
    # empty (picks live in CoreSettings, not the round-tripped PluginConfig
    # settings, so a following action's settings pre-save can't resurrect them).
    reset(
        fields={"remember_ui_picks": True},
        picks={"tmdb:42": {"m3u_account_id": 1, "stream_id": "x"}},
    )
    check("pick present before clear", patch.get_saved_picks() != {})
    patch.clear_all_saved()
    check("get_saved_picks empty right after clear_all", patch.get_saved_picks() == {})
    # Simulate the UI re-saving the stale PluginConfig.settings snapshot (which
    # in the old design still held the pick) -- must NOT bring the pick back.
    DB["settings"]["saved_picks"] = {"tmdb:42": {"m3u_account_id": 1, "stream_id": "x"}}
    patch.invalidate_config_cache()
    check("stale settings re-save does not resurrect pick", patch.get_saved_picks() == {})


def test_legacy_migration():
    print("test_legacy_migration")
    # A pre-0.1.1 pick living in PluginConfig.settings, no CoreSettings row yet.
    reset(
        fields={"prefer_quality": "off", "remember_ui_picks": True},
        picks=None,
        legacy_picks={"tmdb:314": {"m3u_account_id": 5, "stream_id": "leg"}},
    )
    check("legacy pick is read before migration", patch.get_saved_picks().get("tmdb:314") is not None)
    # It should apply on the client play path even before any write.
    r1 = FakeRelation(95, 1, "s1")
    rleg = FakeRelation(96, 5, "leg")
    scenario(FakeMovie(tmdb_id="314"), [r1, rleg])
    _, rel, _ = call()
    check("legacy pick applied before migration", rel.id == rleg.id)
    # First capture migrates everything into CoreSettings.
    scenario(FakeMovie(tmdb_id="999"), [FakeRelation(97, 2, "n1"), FakeRelation(98, 3, "n2")])
    call(preferred_stream_id="n2")
    check("CoreSettings row now exists", CORE["exists"] is True)
    check("migrated legacy pick retained", CORE["value"].get("tmdb:314") is not None)
    check("new pick stored alongside", CORE["value"].get("tmdb:999") == {"m3u_account_id": 3, "stream_id": "n2"})


def test_wrapper_falls_back_on_error():
    print("test_wrapper_falls_back_on_error")
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": True})
    r1 = FakeRelation(80, 1, "s1", quality="1080p")
    r2 = FakeRelation(81, 2, "s2", quality="4K")
    scenario(FakeMovie(tmdb_id="123"), [r1, r2])
    saved_apply = patch._apply_preferences
    try:
        def boom(*a, **k):
            raise RuntimeError("simulated ladder failure")
        patch._apply_preferences = boom
        content, rel, cands = call()
        check("wrapper returns native relation on error", rel.id == r1.id)
        check("wrapper returns native candidates on error", [c.id for c in cands] == [r1.id, r2.id])
    finally:
        patch._apply_preferences = saved_apply


def test_inactive_passes_through():
    print("test_inactive_passes_through")
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": True})
    patch._ACTIVE = False
    r1 = FakeRelation(90, 1, "s1", quality="1080p")
    r2 = FakeRelation(91, 2, "s2", quality="4K")
    scenario(FakeMovie(tmdb_id="123"), [r1, r2])
    _, rel, _ = call()
    check("inactive -> native primary (no re-sort)", rel.id == r1.id)
    patch._ACTIVE = True


def test_signature_parity():
    print("test_signature_parity")
    # A future Dispatcharr release may add a parameter to _get_content_and_relation
    # and pass it (positionally or as a keyword). The wrapper must ACCEPT it (no
    # TypeError while binding args -- that would precede the _ACTIVE guard/try and
    # HTTP-500 every VOD request) AND FORWARD it to the original (else a constraint
    # core relied on is silently dropped). This encodes no parameter list, so it
    # survives this release and the next. Mutation-check: remove *args/**kwargs
    # from patched_get_content_and_relation and this goes red.
    reset(fields={"prefer_quality": "off", "remember_ui_picks": False})
    scenario(FakeMovie(tmdb_id="1"), [FakeRelation(1, 1, "s1")])
    sentinel_pos, sentinel_kw = object(), object()

    # Active path (the main _orig call site).
    NATIVE_CALLS.clear()
    patch.patched_get_content_and_relation(
        "movie", "u", None, None, sentinel_pos, future_param=sentinel_kw)
    check("active: unknown args accepted (no TypeError)", len(NATIVE_CALLS) == 1)
    rec = NATIVE_CALLS[-1] if NATIVE_CALLS else {"args": (), "kwargs": {}}
    check("active: unknown positional forwarded to original", sentinel_pos in rec["args"])
    check("active: unknown keyword forwarded to original",
          rec["kwargs"].get("future_param") is sentinel_kw)

    # Inactive early-return path (must forward there too, or disabling the feature
    # reintroduces the crash).
    NATIVE_CALLS.clear()
    patch._ACTIVE = False
    patch.patched_get_content_and_relation(
        "movie", "u", None, None, sentinel_pos, future_param=sentinel_kw)
    patch._ACTIVE = True
    rec = NATIVE_CALLS[-1] if NATIVE_CALLS else {"args": (), "kwargs": {}}
    check("inactive: unknown positional forwarded", sentinel_pos in rec["args"])
    check("inactive: unknown keyword forwarded", rec["kwargs"].get("future_param") is sentinel_kw)


# --------------------------------------------------------------------------- #
# 1.4.0: video bitrate ("avoid low-bitrate copies")
# --------------------------------------------------------------------------- #
def BR(rid, account_id, stream_id, width=1920, height=1080, codec="h264",
       video_bps=None, total=None, audio=None, episode=True, account_name=None):
    """A relation carrying bitrate info in the real provider shapes.

    episode=True  -> info.info.{video,audio,bitrate}  (provider episode shape;
                     total in kbps as an int)
    episode=False -> detailed_info.{video,audio} + ffprobe_info.bitrate (movie
                     enriched by a probing plugin; total in bps as a string)
    """
    r = FakeRelation(rid, account_id, stream_id, account_name=account_name)
    video = {"codec_name": codec, "width": width, "height": height}
    if video_bps is not None:
        video["tags"] = {"BPS": str(video_bps)}
    if episode:
        inner = {"video": video}
        if audio is not None:
            inner["audio"] = audio
        if total is not None:
            inner["bitrate"] = total
        r.custom_properties = {"info": {"title": "Episode", "info": inner}}
    else:
        props = {"detailed_info": {"video": video}}
        if audio is not None:
            props["detailed_info"]["audio"] = audio
        if total is not None:
            props["ffprobe_info"] = {"bitrate": str(total)}
        r.custom_properties = props
    return r


def EAC3_51():
    a = AUD("eac3", 6)
    a["bit_rate"] = 256000
    return a


FLOORS = dict(patch.DEFAULT_MIN_BITRATE)


def test_bitrate_reading():
    print("test_bitrate_reading")
    # Provider total in kbps minus the audio track (the real low-bitrate case).
    r = BR(1, 1, "a", codec="hevc", total=888, audio=EAC3_51())
    bps, src = patch.video_bitrate_bps(r)
    check("kbps total minus audio", src == "total-audio" and abs(bps - 632000) < 1)
    check("HEVC counted 1.5x", abs(patch.effective_mbps(r) - 0.948) < 0.001)
    # A video-only BPS tag wins over the total (multi-audio files mislead).
    r = BR(2, 1, "b", video_bps=4580299, total=8955, audio=EAC3_51())
    bps, src = patch.video_bitrate_bps(r)
    check("video BPS tag preferred over total", src == "video" and bps == 4580299)
    # ffprobe-style total in bps (string), no audio figure -> total as-is.
    r = BR(3, 1, "c", total=6000000, episode=False)
    bps, src = patch.video_bitrate_bps(r)
    check("bps string total (enriched movie) normalized", src == "total" and bps == 6000000)
    check("same bitrate whether stored as kbps or bps",
          patch.effective_mbps(BR(4, 1, "d", total=6000)) == patch.effective_mbps(r))
    # A video track's own bit_rate field also counts.
    r = BR(5, 1, "e")
    r.custom_properties["info"]["info"]["video"]["bit_rate"] = "3000000"
    check("video stream bit_rate used", patch.video_bitrate_bps(r) == (3000000.0, "video"))
    # Nonsense per-track value ignored; nothing else -> unknown.
    r = BR(6, 1, "f", video_bps=12)
    check("tiny per-track figure ignored -> unknown", patch.effective_mbps(r) is None)
    check("no bitrate anywhere -> None", patch.effective_mbps(FakeRelation(7, 1, "g")) is None)
    check("unknown bitrate -> bucket 0", patch.bitrate_bucket(FakeRelation(7, 1, "g")) == 0)


def test_is_starved():
    print("test_is_starved")
    low = BR(1, 1, "a", codec="hevc", total=888, audio=EAC3_51())      # 0.95 eq @1080p
    ok = BR(2, 1, "b", video_bps=4580299, audio=EAC3_51())             # 4.58 @1080p
    check("0.95 Mbps-eq 1080p is starved at 1.5", patch.is_starved(low, FLOORS) is True)
    check("4.58 Mbps 1080p is fine", patch.is_starved(ok, FLOORS) is False)
    check("unknown bitrate never starved", patch.is_starved(FakeRelation(3, 1, "c", video=VID(1920, 1080)), FLOORS) is False)
    check("unknown tier never starved", patch.is_starved(FakeRelation(4, 1, "d"), FLOORS) is False)
    no_1080 = {k: v for k, v in FLOORS.items() if k != "1080p"}
    check("tier minimum off -> never starved", patch.is_starved(low, no_1080) is False)
    check("floors None -> never starved", patch.is_starved(low, None) is False)
    # HEVC credit matters: 1.2 Mbps HEVC (1.8 eq) clears 1.5; 1.2 Mbps H.264 does not.
    check("1.2 Mbps HEVC clears the 1.5 minimum",
          patch.is_starved(BR(5, 1, "e", codec="hevc", video_bps=1200000), FLOORS) is False)
    check("1.2 Mbps H.264 is below it",
          patch.is_starved(BR(6, 1, "f", codec="h264", video_bps=1200000), FLOORS) is True)


def test_parse_floors():
    print("test_parse_floors")
    check("no settings -> defaults", patch._parse_floors({}) == patch.DEFAULT_MIN_BITRATE)
    f = patch._parse_floors({"min_bitrate_1080p": "2", "min_bitrate_720p": 0,
                             "min_bitrate_4k": "abc", "min_bitrate_480p": ""})
    check("numeric string accepted", f.get("1080p") == 2.0)
    check("0 removes that tier's minimum", "720p" not in f)
    check("invalid -> default", f.get("4k") == patch.DEFAULT_MIN_BITRATE["4k"])
    check("blank -> default", f.get("480p") == patch.DEFAULT_MIN_BITRATE["480p"])


def _bitrate_fields(on=True, **extra):
    fields = {"prefer_quality": "4k", "remember_ui_picks": False, "prefer_audio": True,
              "avoid_low_bitrate": on}
    fields.update(extra)
    return fields


def test_low_bitrate_ranking():
    print("test_low_bitrate_ranking")
    # The motivating case: two 1080p E-AC3 5.1 copies; the higher-priority
    # provider's is a 0.9 Mbps HEVC re-encode.
    low = BR(1, 1, "low", codec="hevc", total=888, audio=EAC3_51())
    good = BR(2, 2, "good", video_bps=4580299, total=4842, audio=EAC3_51())
    ep = FakeEpisode(series_uuid="s1", season_number=1, episode_number=1)

    reset(fields=_bitrate_fields(on=False))
    scenario(ep, [low, good])
    _, rel, _ = call()
    check("setting off -> native order kept (starved copy still first)", rel.id == low.id)

    reset(fields=_bitrate_fields(on=True))
    scenario(ep, [low, good])
    _, rel, cands = call()
    check("setting on -> healthy copy wins", rel.id == good.id)
    check("starved copy kept as failover (never excluded)", [c.id for c in cands] == [good.id, low.id])

    # Never crosses tiers: a starved 1080p still beats a healthy 720p.
    r720 = BR(3, 2, "720", width=1280, height=720, video_bps=3000000, audio=EAC3_51())
    reset(fields=_bitrate_fields(on=True))
    scenario(ep, [r720, low])
    _, rel, _ = call()
    check("starved 1080p still beats healthy 720p (tier first)", rel.id == low.id)

    # Bitrate outranks audio: a healthy 2.0 beats a starved 5.1 in the same tier.
    stereo_ok = BR(4, 2, "st", video_bps=4000000, audio=AUD("aac", 2))
    reset(fields=_bitrate_fields(on=True))
    scenario(ep, [low, stereo_ok])
    _, rel, _ = call()
    check("healthy stereo beats starved 5.1 within a tier", rel.id == stereo_ok.id)

    # Unknown bitrate is never demoted: it beats a starved copy.
    unknown = FakeRelation(5, 2, "unk", video=VID(1920, 1080), audio=EAC3_51())
    reset(fields=_bitrate_fields(on=True))
    scenario(ep, [low, unknown])
    _, rel, _ = call()
    check("unknown bitrate beats a starved copy", rel.id == unknown.id)

    # Needs a quality preference (like the audio tiebreak).
    reset(fields=_bitrate_fields(on=True, prefer_quality="off"))
    scenario(ep, [low, good])
    _, rel, _ = call()
    check("prefer quality off -> setting has no effect", rel.id == low.id)

    # Tier minimum set to 0 -> that tier is never starved.
    reset(fields=_bitrate_fields(on=True, min_bitrate_1080p=0))
    scenario(ep, [low, good])
    _, rel, _ = call()
    check("1080p minimum 0 -> not starved; only the bitrate tiebreak separates them",
          rel.id == good.id and patch.is_starved(low, patch._parse_floors({"min_bitrate_1080p": 0})) is False)


def test_bitrate_tiebreak():
    print("test_bitrate_tiebreak")
    ep = FakeEpisode(series_uuid="s1", season_number=1, episode_number=1)
    a = BR(1, 1, "a", video_bps=3000000, audio=EAC3_51())
    b = BR(2, 2, "b", video_bps=9000000, audio=EAC3_51())
    reset(fields=_bitrate_fields(on=True))
    scenario(ep, [a, b])
    _, rel, _ = call()
    check("clearly higher bitrate wins a full tie", rel.id == b.id)

    # Same release on two providers (identical video track) -> native order.
    c = BR(3, 1, "c", video_bps=4580299, audio=EAC3_51())
    d = BR(4, 2, "d", video_bps=4580299, audio=EAC3_51())
    reset(fields=_bitrate_fields(on=True))
    scenario(ep, [c, d])
    _, rel, _ = call()
    check("identical bitrates tie -> native order", rel.id == c.id)
    check("near-identical encodes share a bucket",
          patch.bitrate_bucket(BR(5, 1, "e", video_bps=4600000)) ==
          patch.bitrate_bucket(BR(6, 1, "f", video_bps=4700000)))

    # Audio still outranks the bitrate tiebreak.
    e = BR(7, 1, "e", video_bps=3000000, audio=EAC3_51())
    f = BR(8, 2, "f", video_bps=9000000, audio=AUD("aac", 2))
    reset(fields=_bitrate_fields(on=True))
    scenario(ep, [f, e])
    _, rel, _ = call()
    check("better audio outranks a higher (healthy) bitrate", rel.id == e.id)

    # Setting off -> the tiebreak is off too.
    reset(fields=_bitrate_fields(on=False))
    scenario(ep, [a, b])
    _, rel, _ = call()
    check("setting off -> no bitrate tiebreak", rel.id == a.id)


def test_show_pick_honors_bitrate():
    print("test_show_pick_honors_bitrate")
    # Saved TV pick = provider 1, which carries a starved and a healthy 1080p copy.
    ep = FakeEpisode(series_uuid="s1", season_number=1, episode_number=2, series_tmdb="77")
    low = BR(1, 1, "low", codec="hevc", total=888, audio=EAC3_51())
    good = BR(2, 1, "good", video_bps=4580299, audio=EAC3_51())
    other = BR(3, 2, "other", video_bps=9000000, audio=EAC3_51())
    reset(fields=_bitrate_fields(on=True, remember_ui_picks=True),
          picks={"stmdb:77": {"m3u_account_id": 1}})
    scenario(ep, [low, other, good])
    _, rel, cands = call()
    check("show pick keeps the provider, healthy copy within it", rel.id == good.id)
    check("other provider only after the picked one", [c.id for c in cands] == [good.id, low.id, other.id])


# --------------------------------------------------------------------------- #
# 1.4.0: category quality tag
# --------------------------------------------------------------------------- #
VOD = {"cat_rows": [], "series_cat": {}, "queries": 0, "fail": False}


class _VodQS:
    def __init__(self, kind, flt):
        self.kind, self.flt = kind, flt

    def _rows(self):
        VOD["queries"] += 1
        if VOD["fail"]:
            raise RuntimeError("db down")
        return VOD["cat_rows"]

    def exists(self):
        return any("quality" in (p or {}) for _a, _c, p in self._rows())

    def values_list(self, *fields):
        if self.kind == "series":
            VOD["queries"] += 1
            ids = self.flt.get("id__in") or set()
            return [(i, VOD["series_cat"].get(i)) for i in ids]
        accts = self.flt.get("m3u_account_id__in") or set()
        cats = self.flt.get("category_id__in") or set()
        return [(a, c, p) for a, c, p in self._rows() if a in accts and c in cats]


class _VodManager:
    def __init__(self, kind):
        self.kind = kind

    def filter(self, **flt):
        return _VodQS(self.kind, flt)


class M3UVODCategoryRelation:
    objects = _VodManager("cat")


class M3USeriesRelation:
    objects = _VodManager("series")


def _install_fake_vod(enabled=True):
    if enabled:
        vod_mod = types.ModuleType("apps.vod")
        vod_models = types.ModuleType("apps.vod.models")
        vod_models.M3UVODCategoryRelation = M3UVODCategoryRelation
        vod_models.M3USeriesRelation = M3USeriesRelation
        sys.modules["apps.vod"] = vod_mod
        sys.modules["apps.vod.models"] = vod_models
    else:
        sys.modules.pop("apps.vod", None)
        sys.modules.pop("apps.vod.models", None)
    patch._cat_tags_cache = None


def _cat_rel(rid, account_id, stream_id, category_id=None, series_relation_id=None, **kw):
    r = FakeRelation(rid, account_id, stream_id, **kw)
    if category_id is not None:
        r.category_id = category_id
    if series_relation_id is not None:
        r.series_relation_id = series_relation_id
    return r


def test_category_tag_absent_changes_nothing():
    print("test_category_tag_absent_changes_nothing")
    # Andy's setup: no category carries a quality tag. Rankings must be exactly
    # as before and the per-request cost must be one cached existence check.
    _install_fake_vod(enabled=True)
    VOD.update(cat_rows=[(1, 10, {}), (2, 20, {"language": "en"})], series_cat={}, queries=0, fail=False)
    a = _cat_rel(1, 1, "a", category_id=10)
    b = _cat_rel(2, 2, "b", category_id=20, name="Example 4K")
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    scenario(FakeMovie(tmdb_id="1"), [a, b])
    _, rel, _ = call()
    check("no tags: name label still decides as before", rel.id == b.id)
    check("no tags: candidates annotated None", getattr(a, patch._CAT_TIER_ATTR, "x") is None)
    first = VOD["queries"]
    scenario(FakeMovie(tmdb_id="1"), [a, b])
    call()
    check("no tags: existence check cached (no further queries)", VOD["queries"] == first)

    # No tags + no other signal -> native order, unchanged.
    c = _cat_rel(3, 1, "c", category_id=10)
    d = _cat_rel(4, 2, "d", category_id=20)
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    scenario(FakeMovie(tmdb_id="1"), [c, d])
    _, rel, cands = call()
    check("no tags + no signal -> native", rel.id == c.id and [x.id for x in cands] == [c.id, d.id])

    # Older Dispatcharr (no apps.vod.models importable) -> silently dormant.
    _install_fake_vod(enabled=False)
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    scenario(FakeMovie(tmdb_id="1"), [c, d])
    _, rel, _ = call()
    check("model unavailable -> native, no error", rel.id == c.id)


def test_category_tag_ranks():
    print("test_category_tag_ranks")
    _install_fake_vod(enabled=True)
    VOD.update(cat_rows=[(1, 10, {"quality": "1080p"}), (2, 20, {"quality": "4K"}),
                         (3, 30, {"quality": "SD"})],
               series_cat={500: 20}, queries=0, fail=False)
    plain = _cat_rel(1, 1, "a", category_id=10)
    tagged4k = _cat_rel(2, 2, "b", category_id=20)
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    scenario(FakeMovie(tmdb_id="1"), [plain, tagged4k])
    _, rel, _ = call()
    check("4K-tagged category wins under Prefer 4K", rel.id == tagged4k.id)
    check("category tier read", patch.quality_tier(tagged4k) == "4k" and patch.quality_tier(plain) == "1080p")
    sd = _cat_rel(3, 3, "c", category_id=30)
    patch._annotate_category_tiers([sd])
    check("SD tag -> 480p", patch.quality_tier(sd) == "480p")

    # Waterfall order: dims and the stream name outrank the category tag;
    # the category tag outranks the account name.
    dims = _cat_rel(4, 2, "d", category_id=20, video=VID(1920, 1080))
    named = _cat_rel(5, 2, "e", category_id=20, name="Example 1080p")
    acct = _cat_rel(6, 1, "f", category_id=10, account_name="Provider 4K")
    patch._annotate_category_tiers([dims, named, acct])
    check("measured dims outrank the category tag", patch.quality_tier(dims) == "1080p")
    check("stream name outranks the category tag", patch.quality_tier(named) == "1080p")
    check("category tag outranks the account name", patch.quality_tier(acct) == "1080p")

    # Episodes read the tag through their series relation.
    ep_rel = _cat_rel(7, 2, "g", series_relation_id=500)
    ep_plain = _cat_rel(8, 1, "h", series_relation_id=999)
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    scenario(FakeEpisode(series_uuid="s1", season_number=1, episode_number=1), [ep_plain, ep_rel])
    _, rel, _ = call()
    check("episode: tag via series relation wins", rel.id == ep_rel.id)

    # A lookup failure fails open (no tiers, no exception, native order).
    VOD["fail"] = True
    patch._cat_tags_cache = (True, __import__("time").time())  # tags "exist"
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
    patch._cat_tags_cache = (True, __import__("time").time())
    x = _cat_rel(9, 1, "x", category_id=10)
    y = _cat_rel(10, 2, "y", category_id=20, name="Example 4K")
    scenario(FakeMovie(tmdb_id="1"), [x, y])
    _, rel, _ = call()
    check("lookup error -> tags ignored, the rest of the ranking still applies", rel.id == y.id)
    VOD["fail"] = False
    _install_fake_vod(enabled=False)


# --------------------------------------------------------------------------- #
# 1.4.0: per-worker registry behind the Status action
# --------------------------------------------------------------------------- #
class FakeRedis:
    def __init__(self):
        self.h = {}

    def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)


def test_worker_registry():
    print("test_worker_registry")
    import json as _json
    fake = FakeRedis()
    orig = (patch._get_redis, patch._process_role, patch._pid_alive)
    try:
        patch._get_redis = lambda: fake
        patch._process_role = lambda: "uwsgi"
        patch._pid_alive = lambda rec: rec.get("pid") != 4242
        patch._register_worker(True)
        rep = patch.worker_report()
        check("registry readable", rep["available"] is True)
        check("this worker listed as active", __import__("os").getpid() in rep["active"])
        # Another (stream) worker recorded inactive, a Celery record, a dead pid, an old record.
        now = __import__("time").time()
        fake.hset(patch.WORKERS_REDIS_KEY, "h:777", _json.dumps({"pid": 777, "host": "h", "role": "uwsgi", "active": False, "ts": now}))
        fake.hset(patch.WORKERS_REDIS_KEY, "h:888", _json.dumps({"pid": 888, "host": "h", "role": "celery", "active": True, "ts": now}))
        fake.hset(patch.WORKERS_REDIS_KEY, "h:4242", _json.dumps({"pid": 4242, "host": "h", "role": "uwsgi", "active": True, "ts": now}))
        fake.hset(patch.WORKERS_REDIS_KEY, "h:999", _json.dumps({"pid": 999, "host": "h", "role": "uwsgi", "active": True, "ts": now - patch.WORKER_RECORD_MAX_AGE - 10}))
        fake.hset(patch.WORKERS_REDIS_KEY, "h:bad", "not json")
        rep = patch.worker_report()
        check("inactive stream worker reported", rep["inactive"] == [777])
        check("celery record not counted as a stream worker", 888 not in rep["active"])
        check("dead pid not counted", 4242 not in rep["active"])
        patch._register_worker(True)  # a write prunes dead / old / garbage records
        fields = set(fake.hgetall(patch.WORKERS_REDIS_KEY))
        check("write prunes dead, stale and unreadable records",
              not ({"h:4242", "h:999", "h:bad"} & fields) and {"h:777", "h:888"} <= fields)
        patch._process_role = lambda: "celery"
        check("report says where the check ran", patch.worker_report()["here"] == "celery")
        patch._get_redis = lambda: None
        check("no Redis -> unavailable, no error", patch.worker_report()["available"] is False)
        patch._register_worker(True)  # must not raise
    finally:
        patch._get_redis, patch._process_role, patch._pid_alive = orig



# --------------------------------------------------------------------------- #
# 1.5.0: avoid rules, recent plays, missed-4K notification
# --------------------------------------------------------------------------- #
class FakeRedis2(FakeRedis):
    """Adds the list / SET NX / pipeline calls the recent-plays and notification
    code use."""

    def __init__(self):
        super().__init__()
        self.lists = {}
        self.kv = {}
        self.reads = 0

    def lrange(self, key, start, end):
        self.reads += 1
        lst = self.lists.get(key, [])
        return list(lst[start:] if end == -1 else lst[start:end + 1])

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    def pipeline(self):
        outer = self

        class _P:
            def __init__(self):
                self.ops = []

            def delete(self, key):
                self.ops.append(("del", key))

            def rpush(self, key, *vals):
                self.ops.append(("rpush", key, vals))

            def execute(self):
                for op in self.ops:
                    if op[0] == "del":
                        outer.lists.pop(op[1], None)
                    else:
                        outer.lists.setdefault(op[1], []).extend(op[2])
        return _P()


def _movie(cid, tmdb, name="Example Movie", year=2020):
    m = FakeMovie(tmdb_id=tmdb)
    m.id = cid
    m.name = name
    m.year = year
    return m


def _episode(cid, series_tmdb, season, number, show="Example Show"):
    e = FakeEpisode(series_uuid="su-" + str(series_tmdb), season_number=season,
                    episode_number=number, series_tmdb=series_tmdb)
    e.id = cid
    e.series.name = show
    return e


def _with_redis(fake):
    orig = patch._get_redis
    patch._get_redis = lambda: fake
    return orig


def call_ep(preferred_stream_id=None, preferred_m3u_account_id=None):
    return patch.patched_get_content_and_relation(
        "episode", "uuid-e", preferred_m3u_account_id, preferred_stream_id
    )


def test_avoid_key_matches():
    print("test_avoid_key_matches")
    check("fake avoid row key == patch.AVOID_CORE_KEY", AVOID_KEY == patch.AVOID_CORE_KEY)


def test_avoid_rule_movie():
    print("test_avoid_rule_movie")
    rule = {"kind": "movie", "key": "tmdb:1", "acct": 1, "stream": "a", "season": None}
    a, b = FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")
    reset(fields={"remember_ui_picks": False}, avoid=[rule])
    scenario(_movie(11, "1"), [a, b])
    _, rel, cands = call()
    check("avoided copy not served", rel.id == b.id)
    check("avoided copy kept as failover (last)", [c.id for c in cands] == [2, 1])

    # A rule for another stream on the same account, or another title, doesn't match.
    reset(fields={"remember_ui_picks": False},
          avoid=[dict(rule, stream="zzz"), dict(rule, key="tmdb:999")])
    scenario(_movie(11, "1"), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")])
    _, rel, _ = call()
    check("non-matching rules leave native choice", rel.id == 1)

    # Stream-fallback key (title with no tmdb/imdb) matches too.
    reset(fields={"remember_ui_picks": False},
          avoid=[dict(rule, key="as:1:a")])
    scenario(_movie(12, None), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")])
    _, rel, _ = call()
    check("as:<acct>:<stream> movie rule matches", rel.id == 2)

    # Every copy avoided -> still plays (demote, never exclude).
    reset(fields={"remember_ui_picks": False},
          avoid=[rule, dict(rule, acct=2, stream="b")])
    scenario(_movie(11, "1"), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")])
    _, rel, cands = call()
    check("all avoided: native first copy still served", rel.id == 1 and len(cands) == 2)


def test_avoid_rule_beats_quality():
    print("test_avoid_rule_beats_quality")
    rule = {"kind": "movie", "key": "tmdb:1", "acct": 1, "stream": "a"}
    a = FakeRelation(1, 1, "a", video=VID(3840, 2160))
    b = FakeRelation(2, 2, "b", video=VID(1920, 1080))
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False}, avoid=[rule])
    scenario(_movie(11, "1"), [a, b])
    _, rel, cands = call()
    check("avoided 4K loses to 1080p under Prefer 4K", rel.id == 2)
    check("avoided 4K is the failover", [c.id for c in cands] == [2, 1])

    # Quality on but no signal at all: native primary kept, avoided copy moved last.
    x, y, z = FakeRelation(1, 1, "x"), FakeRelation(2, 2, "y"), FakeRelation(3, 3, "z")
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False},
          avoid=[{"kind": "movie", "key": "tmdb:1", "acct": 2, "stream": "y"}])
    scenario(_movie(11, "1"), [x, y, z])
    _, rel, cands = call()
    check("no-signal: primary unchanged", rel.id == 1)
    check("no-signal: avoided copy demoted in failover", [c.id for c in cands] == [1, 3, 2])

    # Quality off, avoid rule only, avoided copy not primary -> failover reordered.
    reset(fields={"remember_ui_picks": False},
          avoid=[{"kind": "movie", "key": "tmdb:1", "acct": 2, "stream": "y"}])
    scenario(_movie(11, "1"), [FakeRelation(1, 1, "x"), FakeRelation(2, 2, "y"), FakeRelation(3, 3, "z")])
    _, rel, cands = call()
    check("quality off: failover still demotes avoided", [c.id for c in cands] == [1, 3, 2])


def test_avoid_rule_episodes():
    print("test_avoid_rule_episodes")
    show_rule = {"kind": "episode", "key": "stmdb:55", "acct": 1, "stream": None, "season": None}
    reset(fields={"remember_ui_picks": False}, avoid=[show_rule])
    scenario(_episode(21, "55", 2, 3), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")])
    _, rel, _ = call_ep()
    check("show rule avoids provider", rel.id == 2)

    s1 = dict(show_rule, season=1)
    reset(fields={"remember_ui_picks": False}, avoid=[s1])
    scenario(_episode(21, "55", 2, 3), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")])
    _, rel, _ = call_ep()
    check("season-1 rule ignores season 2", rel.id == 1)
    scenario(_episode(22, "55", 1, 4), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")])
    _, rel, _ = call_ep()
    check("season-1 rule applies to season 1", rel.id == 2)

    reset(fields={"remember_ui_picks": False}, avoid=[dict(show_rule, key="stmdb:66")])
    scenario(_episode(21, "55", 2, 3), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b")])
    _, rel, _ = call_ep()
    check("rule for another show ignored", rel.id == 1)

    # Movie-only rules never touch an episode's series (a lazy DB query).
    class _NoSeries:
        season_number = 1
        episode_number = 1
        tmdb_id = None
        imdb_id = None
        id = 30

        @property
        def series(self):
            raise AssertionError("series touched")
    got = patch._avoided_ids(_NoSeries(), [FakeRelation(1, 1, "a")],
                             [{"kind": "movie", "key": "tmdb:1", "acct": 1, "stream": "a"}])
    check("no episode rules -> no series lookup", got == set())


def test_saved_pick_beats_avoid():
    print("test_saved_pick_beats_avoid")
    rule = {"kind": "movie", "key": "tmdb:1", "acct": 1, "stream": "a"}
    picks = {"tmdb:1": {"m3u_account_id": 1, "stream_id": "a"}}
    reset(fields={"remember_ui_picks": True}, picks=picks,
          avoid=[rule, {"kind": "movie", "key": "tmdb:1", "acct": 2, "stream": "b"}])
    scenario(_movie(11, "1"), [FakeRelation(2, 2, "b"), FakeRelation(1, 1, "a"), FakeRelation(3, 3, "c")])
    _, rel, cands = call()
    check("saved pick served even though avoided", rel.id == 1)
    check("saved pick failover tail demotes avoided", [c.id for c in cands] == [1, 3, 2])

    # TV show pick: other providers' failover demotes avoided ones.
    reset(fields={"remember_ui_picks": True}, picks={"stmdb:55": {"m3u_account_id": 3}},
          avoid=[{"kind": "episode", "key": "stmdb:55", "acct": 1}])
    scenario(_episode(21, "55", 1, 1),
             [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b"), FakeRelation(3, 3, "c")])
    _, rel, cands = call_ep()
    check("show pick served", rel.id == 3)
    check("show pick failover demotes avoided provider", [c.id for c in cands] == [3, 2, 1])


def test_dv_outranks_avoid():
    print("test_dv_outranks_avoid")
    # The only alternative to a user-avoided copy is Profile 5 DV: the avoided
    # (playable) copy is still served; with DV avoidance off the P5 copy wins.
    rule = {"kind": "movie", "key": "tmdb:1", "acct": 1, "stream": "a"}
    hd = FakeRelation(1, 1, "a", video=VID(1920, 1080))
    p5 = FakeRelation(2, 2, "b", video=DV(3840, 2160, compat=0))
    reset(fields={"prefer_quality": "4k", "avoid_dv_no_fallback": True,
                  "remember_ui_picks": False}, avoid=[rule])
    scenario(_movie(11, "1"), [hd, p5])
    _, rel, cands = call()
    check("avoided playable copy beats P5", rel.id == 1 and [c.id for c in cands] == [1, 2])
    reset(fields={"prefer_quality": "4k", "remember_ui_picks": False}, avoid=[rule])
    scenario(_movie(11, "1"), [FakeRelation(1, 1, "a", video=VID(1920, 1080)),
                               FakeRelation(2, 2, "b", video=DV(3840, 2160, compat=0))])
    _, rel, _ = call()
    check("DV avoidance off: P5 copy served over the avoided one", rel.id == 2)


def test_played_series():
    print("test_played_series")
    check("fake played row key == patch.PLAYED_CORE_KEY", PLAYED_KEY == patch.PLAYED_CORE_KEY)
    import time as _time
    fake = FakeRedis2()
    orig = _with_redis(fake)
    try:
        reset(fields={"remember_ui_picks": False})
        ep = _episode(21, "55", 2, 5)
        ep.series_id = 7
        scenario(ep, [FakeRelation(1, 1, "a")])
        call_ep()
        rec = patch.get_played_series().get("7")
        check("episode play recorded by Series.id", rec is not None and rec["season"] == 2
              and rec["episode"] == 5 and rec["episode_id"] == 21
              and (rec["max_season"], rec["max_episode"]) == (2, 5))
        check("versioned format", PLAYED["value"].get("version") == patch.PLAYED_FORMAT_VERSION)
        w = PLAYED["writes"]
        call_ep()
        check("repeat of the same play -> no DB write", PLAYED["writes"] == w)

        back = _episode(20, "55", 1, 3)
        back.series_id = 7
        scenario(back, [FakeRelation(1, 1, "z")])
        call_ep()
        rec = patch.get_played_series()["7"]
        check("rewatch of an earlier episode: latest updated, furthest kept",
              (rec["season"], rec["episode"]) == (1, 3)
              and (rec["max_season"], rec["max_episode"]) == (2, 5))

        scenario(_movie(11, "1"), [FakeRelation(1, 1, "m")])
        call()
        check("movie play doesn't touch the record", set(patch.get_played_series()) == {"7"})

        PLAYED["value"]["series"]["99"] = {"ts": _time.time() - (patch.PLAYED_RETENTION_DAYS + 1) * 86400}
        PLAYED["value"]["series"]["98"] = "junk"
        nxt = _episode(22, "56", 1, 1)
        nxt.series_id = 8
        scenario(nxt, [FakeRelation(1, 1, "n")])
        call_ep()
        check("old and malformed entries pruned on write",
              set(patch.get_played_series()) == {"7", "8"})

        # Series id from the related object when series_id isn't loaded.
        reset(fields={"remember_ui_picks": False})
        ep2 = _episode(30, "60", 3, 1)
        ep2.series.id = 12
        scenario(ep2, [FakeRelation(1, 1, "q")])
        call_ep()
        check("falls back to series.id", "12" in patch.get_played_series())
    finally:
        patch._get_redis = orig

    # No Redis: per-process repeat detection still prevents repeated writes.
    reset(fields={"remember_ui_picks": False})
    ep = _episode(21, "55", 2, 5)
    ep.series_id = 7
    scenario(ep, [FakeRelation(1, 1, "a")])
    call_ep()
    w = PLAYED["writes"]
    call_ep()
    check("no Redis: recorded once, repeat skipped", w == 1 and PLAYED["writes"] == 1)

    # A DB failure never affects the play.
    orig_w = patch._write_played
    try:
        def boom(m):
            raise RuntimeError("db down")
        patch._write_played = boom
        reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
        scenario(ep, [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b", quality="4K")])
        _, rel, _ = call_ep()
        check("played-record failure: preferences still applied", rel.id == 2)
    finally:
        patch._write_played = orig_w


def test_avoid_lookup_failure_fails_open():
    print("test_avoid_lookup_failure_fails_open")
    orig = patch._avoided_ids
    try:
        def boom(*a, **k):
            raise RuntimeError("db down")
        patch._avoided_ids = boom
        reset(fields={"prefer_quality": "4k", "remember_ui_picks": False})
        scenario(_movie(11, "1"), [FakeRelation(1, 1, "a"), FakeRelation(2, 2, "b", quality="4K")])
        _, rel, _ = call()
        check("avoid lookup error: quality rule still applies", rel.id == 2)
    finally:
        patch._avoided_ids = orig


def test_recent_plays_record():
    print("test_recent_plays_record")
    import json as _json
    fake = FakeRedis2()
    orig = _with_redis(fake)
    try:
        reset(fields={"remember_ui_picks": False})
        scenario(_movie(11, "1"), [FakeRelation(1, 1, "a")])
        call()
        plays = patch.get_recent_plays()
        check("one play recorded", len(plays) == 1 and plays[0]["cid"] == 11
              and plays[0]["acct"] == 1 and plays[0]["stream"] == "a" and plays[0]["type"] == "movie")
        ts0 = plays[0]["ts"]
        call()
        plays = patch.get_recent_plays()
        check("repeat of the same play not re-recorded", len(plays) == 1 and plays[0]["ts"] == ts0)

        scenario(_episode(21, "55", 2, 3), [FakeRelation(5, 2, "e")])
        call_ep()
        plays = patch.get_recent_plays()
        check("second title newest first", [p["cid"] for p in plays] == [21, 11]
              and plays[0]["type"] == "episode")

        scenario(_movie(11, "1"), [FakeRelation(2, 2, "b")])
        call()
        plays = patch.get_recent_plays()
        check("replayed title (other copy) moves to front, no duplicate",
              [p["cid"] for p in plays] == [11, 21] and plays[0]["stream"] == "b")

        # Same title and copy, but the newest entry is old -> refreshed.
        raw = _json.loads(fake.lists[patch.RECENT_REDIS_KEY][0])
        raw["ts"] -= patch.RECENT_REPEAT_SECONDS + 5
        fake.lists[patch.RECENT_REDIS_KEY][0] = _json.dumps(raw)
        call()
        check("old repeat re-recorded with a new time",
              patch.get_recent_plays()[0]["ts"] > raw["ts"] + 1)

        for i in range(7):
            scenario(_movie(100 + i, str(100 + i)), [FakeRelation(1, 1, "a")])
            call()
        plays = patch.get_recent_plays()
        check("capped at RECENT_MAX distinct titles",
              len(plays) == patch.RECENT_MAX and plays[0]["cid"] == 106
              and len(fake.lists[patch.RECENT_REDIS_KEY]) == patch.RECENT_MAX)
    finally:
        patch._get_redis = orig
    # No Redis: recording is silently skipped.
    reset(fields={"remember_ui_picks": False})
    scenario(_movie(11, "1"), [FakeRelation(1, 1, "a")])
    _, rel, _ = call()
    check("no Redis: play still served", rel.id == 1)


# Fake library for the action helpers: apps.vod.models Movie/Episode, M3UAccount.
LIB = {"movies": {}, "episodes": {}, "accounts": {}}


class _LibQS:
    def __init__(self, table):
        self.table = table
        self.match = None

    def select_related(self, *a):
        return self

    def filter(self, id=None, id__in=None, **kw):
        self.match = [id] if id is not None else list(id__in or [])
        return self

    def first(self):
        for i in self.match or []:
            if i in self.table:
                return self.table[i]
        return None

    def values_list(self, *fields):
        return [(i, self.table[i]) for i in self.match if i in self.table]


class _LibModel:
    def __init__(self, table):
        self._table = table

    @property
    def objects(self):
        return _LibQS(LIB[self._table])


def _install_fake_library():
    vod_models = sys.modules.get("apps.vod.models")
    if vod_models is None:
        sys.modules["apps.vod"] = types.ModuleType("apps.vod")
        vod_models = types.ModuleType("apps.vod.models")
        sys.modules["apps.vod.models"] = vod_models
    vod_models.Movie = _LibModel("movies")
    vod_models.Episode = _LibModel("episodes")
    sys.modules["apps.m3u"] = types.ModuleType("apps.m3u")
    m3u_models = types.ModuleType("apps.m3u.models")
    m3u_models.M3UAccount = _LibModel("accounts")
    sys.modules["apps.m3u.models"] = m3u_models


def _seed_recent(fake, entries):
    import json as _json
    import time as _time
    fake.lists[patch.RECENT_REDIS_KEY] = [
        _json.dumps(dict(e, ts=e.get("ts", _time.time()))) for e in entries]


def test_avoid_from_recent():
    print("test_avoid_from_recent")
    _install_fake_library()
    ep = _episode(21, "55", 2, 3, show="Example Show")
    mv = _movie(11, "1", name="Example Movie", year=2020)
    LIB.update(movies={11: mv}, episodes={21: ep}, accounts={1: "Provider A", 2: "Provider B"})
    fake = FakeRedis2()
    orig = _with_redis(fake)
    try:
        reset(fields={})
        _seed_recent(fake, [
            {"type": "episode", "cid": 21, "acct": 1, "stream": "e1"},
            {"type": "movie", "cid": 11, "acct": 2, "stream": "m2"},
            {"type": "movie", "cid": 999, "acct": 2, "stream": "gone"},
        ])
        rows = patch.describe_recent_plays()
        check("recent plays labeled", [r["label"] for r in rows][:2]
              == ["Example Show S02E03", "Example Movie (2020)"]
              and rows[0]["account"] == "Provider A")
        msg = patch.format_recent_plays(rows)
        check("recent list is one line with numbers", "\n" not in msg and "#1 " in msg and "#3 " in msg)

        res = patch.add_avoid_from_recent(1)
        rules = patch.get_avoid_rules()
        check("episode play -> show-wide provider rule",
              res["ok"] and len(rules) == 1 and rules[0]["kind"] == "episode"
              and rules[0]["key"] == "stmdb:55" and rules[0]["acct"] == 1
              and rules[0]["season"] is None and rules[0]["label"] == "Example Show")
        res = patch.add_avoid_from_recent(1)
        check("duplicate rule not added", res["ok"] and "Already" in res["message"]
              and len(patch.get_avoid_rules()) == 1)
        res = patch.add_avoid_from_recent("1", season_only=True)
        rules = patch.get_avoid_rules()
        check("season-only rule is a separate rule", len(rules) == 2 and rules[1]["season"] == 2)

        res = patch.add_avoid_from_recent(2.0)
        rules = patch.get_avoid_rules()
        check("movie play -> exact-copy rule", res["ok"] and rules[-1]["kind"] == "movie"
              and rules[-1]["key"] == "tmdb:1" and rules[-1]["stream"] == "m2" and rules[-1]["acct"] == 2)

        check("vanished title -> error", not patch.add_avoid_from_recent(3)["ok"])
        check("out of range -> error", not patch.add_avoid_from_recent(9)["ok"]
              and not patch.add_avoid_from_recent(0)["ok"]
              and not patch.add_avoid_from_recent("x")["ok"])

        # The new rules take effect on the next play.
        reset(fields={"remember_ui_picks": False}, avoid=patch.get_avoid_rules())
        scenario(_episode(23, "55", 4, 1), [FakeRelation(1, 1, "e1"), FakeRelation(2, 2, "x")])
        _, rel, _ = call_ep()
        check("rule from recent play applies to another episode", rel.id == 2)

        # Removal.
        n_before = len(AVOID["value"]["rules"])
        listing = patch.format_avoid_rules(patch.get_avoid_rules())
        check("avoid list newest first", listing.index("#%d " % n_before) < listing.index("#1 "))
        first, second = patch.get_avoid_rules()[:2]
        res = patch.remove_avoid_rule(1)
        left = patch.get_avoid_rules()
        check("remove #1 removes the oldest rule", res["ok"] and len(left) == n_before - 1
              and left[0] == second and first not in left)
        check("remove out of range -> error", not patch.remove_avoid_rule(50)["ok"]
                                              and not patch.remove_avoid_rule(0)["ok"])
        left = len(patch.get_avoid_rules())
        check("clear all removes every rule", left > 0 and patch.clear_all_avoid() == left
              and patch.get_avoid_rules() == [])
        check("clear all on none -> 0", patch.clear_all_avoid() == 0)
    finally:
        patch._get_redis = orig


def test_avoid_clears_conflicting_pick():
    print("test_avoid_clears_conflicting_pick")
    _install_fake_library()
    ep = _episode(21, "55", 2, 3)
    mv = _movie(11, "1")
    LIB.update(movies={11: mv}, episodes={21: ep}, accounts={1: "Provider A", 2: "Provider B"})
    fake = FakeRedis2()
    orig = _with_redis(fake)
    try:
        reset(fields={}, picks={"tmdb:1": {"m3u_account_id": 2, "stream_id": "m2"},
                                "stmdb:55": {"m3u_account_id": 1}})
        _seed_recent(fake, [{"type": "movie", "cid": 11, "acct": 2, "stream": "m2"},
                            {"type": "episode", "cid": 21, "acct": 1, "stream": "e1"}])
        res = patch.add_avoid_from_recent(1)
        check("movie: conflicting saved pick removed", "tmdb:1" not in patch.get_saved_picks()
              and "Removed the saved pick" in res["message"])
        res = patch.add_avoid_from_recent(2, season_only=True)
        check("season rule: show pick kept, user told", "stmdb:55" in patch.get_saved_picks()
              and "outranks" in res["message"])
        res = patch.add_avoid_from_recent(2)
        check("show rule: conflicting show pick removed", "stmdb:55" not in patch.get_saved_picks())
    finally:
        patch._get_redis = orig


def test_popup_fits():
    print("test_popup_fits")
    long = "An Extremely Long Title That Goes On And On Forever"
    rows = [{"n": i, "label": long, "account": "A Provider With A Long Name", "ago": "5m"}
            for i in range(1, patch.RECENT_MAX + 1)]
    msg = patch.format_recent_plays(rows)
    check("labels truncated with an ellipsis", "…" in msg and long not in msg)
    check("5 plays fit a short popup (<= 330 chars)", len(msg) <= 330)
    ep = patch.format_recent_plays([{"n": 1, "label": long + " S12E34", "account": "P", "ago": "1m"}])
    check("long show name keeps its SxxEyy", "S12E34" in ep and long not in ep)
    check("short labels untouched", "Show S01E02" in patch.format_recent_plays(
        [{"n": 1, "label": "Show S01E02", "account": "P", "ago": "1m"}]))
    rules = [{"kind": "episode", "key": "stmdb:%d" % i, "acct": 1, "label": long,
              "acct_name": "Provider", "season": None} for i in range(20)]
    lst = patch.format_avoid_rules(rules)
    check("avoid list capped", lst.count("#") == patch.AVOID_LIST_SHOWN and "newest" in lst
          and "#20 " in lst and "#1 " not in lst)


def _p5_4k(rid, acct):
    return FakeRelation(rid, acct, "s%d" % rid, video=DV(3840, 2160, compat=0))


def test_missed_4k():
    print("test_missed_4k")
    sent = []
    orig = patch._notify_async
    patch._notify_async = lambda cfg, title, msg: sent.append((title, msg))
    base = {"prefer_quality": "4k", "avoid_dv_no_fallback": True, "notify_missed_4k": True,
            "prowl_api_key": "k", "remember_ui_picks": False}
    try:
        p5 = _p5_4k(1, 1)
        hd = FakeRelation(2, 2, "b", video=VID(1920, 1080), account_name="Provider B")
        reset(fields=base)
        scenario(_movie(11, "1", name="Example Movie", year=2020), [p5, hd])
        _, rel, _ = call()
        check("P5-only 4K -> 1080p served", rel.id == 2)
        check("alert sent once", len(sent) == 1 and sent[0][0] == "Missed 4K"
              and "Example Movie (2020)" in sent[0][1] and "1080p" in sent[0][1]
              and "Provider B" in sent[0][1])
        call()
        check("same title not re-alerted", len(sent) == 1)

        sent.clear()
        reset(fields=base)
        scenario(_episode(21, "55", 2, 3, show="Example Show"), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))])
        call_ep()
        check("episode alert labeled", len(sent) == 1 and "Example Show S02E03" in sent[0][1])

        def run(fields, cands, content=None, fn=call):
            sent.clear()
            reset(fields=fields)
            scenario(content or _movie(12, "2"), cands)
            fn()
            return len(sent)
        hdr4k = FakeRelation(3, 3, "c", video=VID(3840, 2160))
        check("compatible 4K exists -> no alert",
              run(base, [_p5_4k(1, 1), hdr4k, FakeRelation(2, 2, "b", video=VID(1920, 1080))]) == 0)
        sent.clear()
        reset(fields=base, avoid=[{"kind": "movie", "key": "tmdb:2", "acct": 3, "stream": "c"}])
        scenario(_movie(12, "2"), [_p5_4k(1, 1), FakeRelation(3, 3, "c", video=VID(3840, 2160)),
                                   FakeRelation(2, 2, "b", video=VID(1920, 1080))])
        call()
        check("avoided compatible 4K: no P5 alert", len(sent) == 0)
        check("notify off -> none",
              run(dict(base, notify_missed_4k=False), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))]) == 0)
        check("no key or webhook -> none",
              run(dict(base, prowl_api_key=""), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))]) == 0)
        check("webhook alone is enough",
              run(dict(base, prowl_api_key="", webhook_url="http://x"), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))]) == 1)
        check("Prefer 1080p -> none",
              run(dict(base, prefer_quality="1080p"), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))]) == 0)
        check("DV avoidance off -> P5 served, none",
              run(dict(base, avoid_dv_no_fallback=False), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))]) == 0)
        check("no 4K at all -> none",
              run(base, [FakeRelation(2, 2, "b", video=VID(1920, 1080))]) == 0)
        sent.clear()
        reset(fields=dict(base, remember_ui_picks=True), picks={"tmdb:2": {"m3u_account_id": 2, "stream_id": "b"}})
        scenario(_movie(12, "2"), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))])
        call()
        check("saved pick -> none", len(sent) == 0)
        sent.clear()
        reset(fields=base)
        scenario(_movie(12, "2"), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))])
        call(preferred_stream_id="b")
        check("request pick -> none", len(sent) == 0)
    finally:
        patch._notify_async = orig


def test_missed_4k_dedupe_redis_and_async():
    print("test_missed_4k_dedupe_redis_and_async")
    import threading as _th
    fake = FakeRedis2()
    orig_r = _with_redis(fake)
    got = []
    done = _th.Event()

    def sender(prowl, hook, title, msg):
        got.append((prowl, hook, title, msg, _th.current_thread().name))
        done.set()
    patch._notify_sender = sender
    try:
        base = {"prefer_quality": "4k", "avoid_dv_no_fallback": True, "notify_missed_4k": True,
                "prowl_api_key": "key1", "webhook_url": "http://hook", "remember_ui_picks": False}
        reset(fields=base)
        scenario(_movie(11, "1"), [_p5_4k(1, 1), FakeRelation(2, 2, "b", video=VID(1920, 1080))])
        call()
        done.wait(2)
        check("sent in a background thread with both channels",
              len(got) == 1 and got[0][0] == "key1" and got[0][1] == "http://hook"
              and got[0][4] == "vodpref-notify")
        check("dedupe key written to Redis with NX",
              any(k.startswith(patch.NOTIFY_DEDUPE_PREFIX) for k in fake.kv))
        patch._notified_local.clear()   # another worker: only Redis remembers
        done.clear()
        call()
        done.wait(0.3)
        check("Redis dedupe holds across workers", len(got) == 1)
    finally:
        patch._notify_sender = None
        patch._get_redis = orig_r


def test_send_notification():
    print("test_send_notification")
    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code = code

    codes = {"prowl": 200, "hook": 500}

    def post(url, data=None, json=None, timeout=None):
        calls.append({"url": url, "data": data, "json": json, "timeout": timeout})
        if url == "http://raise":
            raise ConnectionError("nope")
        return Resp(codes["prowl"] if url == patch.PROWL_URL else codes["hook"])

    prev = sys.modules.get("requests")
    sys.modules["requests"] = types.SimpleNamespace(post=post)
    try:
        check("nothing configured -> no calls", patch.send_notification("", "", "t", "m") == []
              and not calls)
        res = patch.send_notification("abc", "http://hook", "Missed 4K", "msg")
        check("prowl ok, webhook 500 reported failed",
              res == [("prowl", True, "HTTP 200"), ("webhook", False, "HTTP 500")])
        check("prowl form fields", calls[0]["data"]["apikey"] == "abc"
              and calls[0]["data"]["event"] == "Missed 4K" and calls[0]["data"]["description"] == "msg")
        check("webhook JSON fields", calls[1]["json"]["title"] == "Missed 4K"
              and calls[1]["json"]["message"] == "msg" and calls[1]["json"]["event"] == "missed_4k"
              and "msg" in calls[1]["json"]["text"] and calls[1]["json"]["content"] == calls[1]["json"]["text"])
        check("short timeouts", all(c["timeout"] == patch.NOTIFY_TIMEOUT_SECONDS for c in calls))
        res = patch.send_notification("", "http://raise", "t", "m")
        check("exception -> reported, not raised", res == [("webhook", False, "ConnectionError")])
    finally:
        if prev is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = prev


if __name__ == "__main__":
    test_quality_helpers()
    test_cover_image_dims_ignored()
    test_quality_signal_sources()
    test_account_name_ranks_episodes()
    test_multi_relation_episode_real_shape()
    test_quality_rule_reorders()
    test_prefer_1080p_differs()
    test_prefer_720p_order()
    test_dims_cinemascope_tolerance()
    test_audio_rank()
    test_audio_tiebreak_within_tier()
    test_audio_decides_when_no_video_signal()
    test_dv_no_fallback_detection()
    test_avoid_dv_prefers_compatible()
    test_avoid_dv_off_keeps_dv()
    test_avoid_dv_with_quality_off()
    test_quality_stable_for_unknowns()
    test_capture_and_persist_once()
    test_episode_capture_is_show_level()
    test_show_level_pick_applies_across_episodes()
    test_show_pick_composes_with_quality_same_account()
    test_saved_pick_applied_on_emby_path()
    test_saved_pick_dropped_when_stream_gone()
    test_stream_fallback_key()
    test_remember_off_disables_feature()
    test_ladder_precedence()
    test_clear_operations()
    test_clear_all_actually_clears()
    test_legacy_migration()
    test_wrapper_falls_back_on_error()
    test_inactive_passes_through()
    test_signature_parity()
    test_bitrate_reading()
    test_is_starved()
    test_parse_floors()
    test_low_bitrate_ranking()
    test_bitrate_tiebreak()
    test_show_pick_honors_bitrate()
    test_category_tag_absent_changes_nothing()
    test_category_tag_ranks()
    test_worker_registry()
    test_avoid_key_matches()
    test_avoid_rule_movie()
    test_avoid_rule_beats_quality()
    test_avoid_rule_episodes()
    test_saved_pick_beats_avoid()
    test_dv_outranks_avoid()
    test_avoid_lookup_failure_fails_open()
    test_recent_plays_record()
    test_played_series()
    test_avoid_from_recent()
    test_avoid_clears_conflicting_pick()
    test_popup_fits()
    test_missed_4k()
    test_missed_4k_dedupe_redis_and_async()
    test_send_notification()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {_failures}")
        sys.exit(1)
    print("All checks passed.")
