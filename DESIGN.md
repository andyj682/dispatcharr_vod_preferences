# dispatcharr_vod_preferences — design & build handoff

A second Dispatcharr plugin (sibling to `dispatcharr_vod_concurrency_fix`).
Goal: give control over **which stream** Dispatcharr serves for a VOD title when
proxying to a client (esp. Emby), where today you only get account/provider
priority. Two features:

1. **Prefer 4K** (rule-based quality prioritization; configurable order).
2. **Remember my UI pick** — when you pick a specific stream in the Dispatcharr
   UI, persist it as that title's default so the Emby/proxy path uses it too
   (solves "the default stream is broken / has wrong AR — let me pick a good one
   once from the UI and have it stick").

Reference the sibling plugin (`../dispatcharr_vod_concurrency_fix/`) for plugin
structure, install/reload-safe monkeypatch pattern, greenlet-local context,
`stop()` revert, self-test harness, and packaging (LICENSE, .gitattributes eol=lf,
POSIX zip built via Python not PowerShell Compress-Archive). Runtime facts are in
the `dispatcharr-runtime-facts` memory (uWSGI 4 workers, lazy-apps, gevent,
enable+restart to propagate).

## How VOD stream selection works today (verified in source)

`apps/proxy/vod_proxy/views.py`:
- `_get_content_and_relation(content_type, content_id, preferred_m3u_account_id,
  preferred_stream_id)` (~L34) builds `candidates` = active
  `M3UMovieRelation` / `M3UEpisodeRelation` rows, ordered **only** by
  `-m3u_account__priority, id` (pure provider priority). It honors an explicit
  `preferred_stream_id` / `preferred_m3u_account_id` (returns that relation), else
  returns `candidates[0]`. Returns `(content_obj, relation, candidates)`.
- `_order_candidates(candidates, preferred_relation)` (~L248) just moves the
  preferred relation to the front (in-memory).
- `stream_vod` calls `_get_content_and_relation` by **module-global name** (~L598)
  and reads `preferred_stream_id = request.GET.get('stream_id')`,
  `preferred_m3u_account_id = request.GET.get('m3u_account_id')`.

There is **no per-stream `order`/`enabled`/`quality` field** in the schema. Adding
real per-title ordering UI (channel-style drag) is a *core* change; this plugin
delivers the practical subset without frontend work.

## Step-0 verification (DONE, 2026-07-29) — auto-capture is feasible

Playing a specific stream from the UI hits
`/proxy/vod/movie/<uuid>?stream_id=<id>`, and `stream_id` **survives the 301
redirect** (second request: `.../<session>?stream_id=<id>`). Native logs
`[VOD-PARAM] Preferred stream ID: <id>` → `[STREAM-SELECTED] Using specific
stream: <id> from provider: <name>`. So the UI conveys the pick to the server on
every play. `_get_content_and_relation` is reached on **both** the UI
`/proxy/vod/` path and the Emby XC path (native `stream_vod` calls it by name), so
one hook covers capture (UI) and application (Emby).

## 4K / quality signal (reuse — don't reinvent)

`apps/vod/serializers.py::get_quality_info` (M3UMovieRelationSerializer ~L148,
episode equivalent ~L216) computes the UI's quality label via a waterfall:
1. relation `custom_properties['quality']` or `['resolution']` (**per-stream,
   provider-supplied** — this is the source of the per-stream "4K stream" labels),
2. movie-level `video` width/height,
3. movie **name** parsing ('4K'/'2160p'/'1080p'/…),
4. movie-level bitrate.

Only #1 varies per stream; #2–4 are movie-level (same for all streams of a title).
So key the 4K rule on #1 (relation.custom_properties). Provide a small
`quality_rank(relation)` → {4K:4, 1080p:3, 720p:2, 480p:1, unknown:0}. Unknown
must be a **stable** tiebreak (keep native account-priority order among equals).

## Hook design

Patch **`views._get_content_and_relation`** (module global; reload-safe capture of
the original, tag patched fn, revert in `stop()` — same pattern as the sibling
plugin). Wrapper: call original → get `(content_obj, relation, candidates)` → apply
the ladder → return possibly-overridden `(relation, candidates)`.

Selection ladder (most-specific first):
1. **Explicit request pick** (`preferred_stream_id`/`m3u_account_id` present):
   original already returns it → **persist** it as this title's default (if
   `remember_ui_picks` on), return as-is.
2. **Saved per-title default**: find that (account_id, stream_id) in `candidates`;
   if present, make it primary + front. If gone (provider dropped it) → fall
   through, never hard-fail.
3. **Quality rule** (`prefer_quality`): stable-sort `candidates` by
   `quality_rank` desc; primary = best. Failover order then also quality-ranked.
4. **Native** account-priority (unchanged).

How does the wrapper see `preferred_stream_id`? It's an argument to
`_get_content_and_relation` — the wrapper receives it. Persist **both**
`m3u_account_id` and `stream_id` (stream_id is unique only per account).

Note: no need to touch `stream_vod`/`_get_m3u_profile`/reservation → composes
cleanly with `dispatcharr_vod_concurrency_fix` (disjoint functions).

## Persistence

- Store in the plugin's own `PluginConfig.settings` JSON (DB-backed → durable
  across restarts; Dispatcharr's Redis is TTL/ephemeral). Map:
  `{ title_key: {"m3u_account_id": <id>, "stream_id": "<id>"} }`.
- Write only on an explicit UI pick (infrequent → a DB write there is fine). Use
  `PluginManager`/`PluginConfig` to update; be careful to merge, not clobber,
  concurrent settings.
- **Title key (survives UUID-regenerating refresh, issues #961/#973):**
  `tmdb_id` → else `imdb_id` → else `f"{m3u_account_id}:{stream_id}"`. For
  episodes: tmdb/imdb → else series-uuid + `SxxExx`.

## Config

`fields`:
- `prefer_quality`: select — `off` / `4K first` / `1080p first` (or text order
  `4K,1080p,720p,480p`).
- `remember_ui_picks`: boolean (default true).

`actions`:
- `list_saved` — return the saved-picks map.
- `clear_saved` — wipe all saved picks (confirm).
- `clear_title` — clear one (param: title key or tmdb/imdb).

## Data model facts (apps/vod/models.py)

`M3UMovieRelation` / `M3UEpisodeRelation`: `movie`/`episode` FK, `m3u_account` FK,
`stream_id`, `container_extension`, `custom_properties` (JSON), `last_seen`;
`unique_together=(m3u_account, stream_id)`; `get_stream_url()`. `Movie`/`Episode`
have `uuid`, `name`, `tmdb_id`, `imdb_id`, `custom_properties`. Relations reached
via `content_obj.m3u_relations`.

## Build order

1. Scaffold (plugin.json/plugin.py/patch.py/README/LICENSE/.gitignore/.gitattributes)
   mirroring the sibling plugin; git init with andyj682 identity + noreply email
   (`11036791+andyj682@users.noreply.github.com`).
2. `quality_rank` + prefer-4K re-sort (works with zero UI dependency — ship first).
3. Add capture + persistence + saved-default application.
4. Self-test (`test_logic.py`) with fake relations/candidates asserting the ladder.
5. Verify live: prefer-4K changes the Emby pick; a UI pick sticks for Emby.

## Open questions to resolve while building

- Exact shape of `custom_properties` for quality on this instance (dump a few
  relations' `custom_properties` to see whether it's `quality`, `resolution`, or
  provider-specific keys — adjust `quality_rank` to match).
- Whether `m3u_account_id` is also passed by the UI in some cases (saw
  `stream_id`; handle both).
