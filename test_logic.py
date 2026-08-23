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


class _CoreRow:
    def __init__(self):
        self.value = dict(CORE["value"])

    def save(self, update_fields=None):
        CORE["value"] = dict(self.value)
        CORE["exists"] = True


class _CoreQS:
    def __init__(self):
        self._values = False

    def values(self, *fields):
        self._values = True
        return self

    def select_for_update(self):
        return self

    def first(self):
        if not CORE["exists"]:
            return None
        if self._values:
            return {"value": dict(CORE["value"])}
        return _CoreRow()


class _CoreManager:
    def filter(self, key=None, **kw):
        return _CoreQS()

    def select_for_update(self):
        return _CoreManagerSFU()

    def create(self, key=None, name=None, value=None):
        CORE["exists"] = True
        CORE["value"] = dict(value or {})
        return _CoreRow()


class _CoreManagerSFU:
    def filter(self, key=None, **kw):
        return _CoreQS()


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
                 name=None, info_title=None, video=None, account_name=None):
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
            props["detailed_info"] = {"video": video}
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


# --------------------------------------------------------------------------- #
# Native stub for _get_content_and_relation
# --------------------------------------------------------------------------- #
CURRENT = {"content": None, "candidates": []}


def native_stub(content_type, content_id, preferred_m3u_account_id=None, preferred_stream_id=None):
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
def reset(fields=None, picks=None, legacy_picks=None):
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
    patch.invalidate_config_cache()
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
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {_failures}")
        sys.exit(1)
    print("All checks passed.")
