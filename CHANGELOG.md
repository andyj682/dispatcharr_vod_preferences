# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-11-27

### Added

- **Avoid a bad copy.** Mark a copy that plays badly (out-of-sync audio, the
  wrong cut, burned-in subtitles and so on) so it's served only if nothing else
  plays. Pick it from **List recent plays** (the last 5 titles the proxy served,
  with the provider each came from) and click **Avoid copy**.
  - TV: avoids that provider for the whole show, or only that season with
    **Avoid just that season (TV)**.
  - Movies: avoids that exact copy.
  - Demotes, never removes: the copy stays at the end of the failover list.
  - Ranks below **Avoid Dolby Vision without HDR/SDR fallback** (a bad copy still
    beats one that won't play properly) and above the quality rule.
  - An explicit pick (a UI play or a saved pick) still wins. Adding a rule removes
    a saved pick that chose the same copy or provider.
  - **List avoid rules**, **Remove rule** and **Clear all avoid rules** to
    manage them. Rules live in their own database row and survive a plugin
    delete, like saved picks.
- **Played-shows record** for other plugins: a durable database row
  (`dispatcharr_vod_preferences_played`) listing which shows were watched in the
  last 60 days, the latest episode played and the furthest one reached. Written
  once per new episode play. Lets an enrichment or pre-conversion plugin focus on
  the next episodes of shows actually being watched. Format documented in the
  README.
- **Missed-4K alert** (opt-in). A Prowl and/or webhook notification when a title
  plays below 4K because its only 4K copy is Dolby Vision without an HDR/SDR
  fallback. At most one per title per day (de-duplicated across workers), sent in
  the background with a 5-second timeout, never for a copy you chose yourself.
  **Send test** checks the setup.

### Changed

- Shorter plugin description on the Plugins page.
- **Check status** also reports the number of avoid rules and played shows, and
  whether the alert is on (and warns when it's on with nowhere to send).
- `tools/rank_dryrun.sh` marks candidates an avoid rule covers (`AVOIDED`) and
  ranks with them, and shows each copy's codec next to its bitrate (plus the
  actual bitrate when a codec credit applies, e.g. `bitrate=2.34 (hevc 1.56 actual)`).

### Fixed

- `tools/rank_dryrun.sh` and `tools/wanted_survey.sh` no longer leave a
  root-owned `__pycache__` folder inside the installed plugin. They run as root
  through `docker exec`, and that leftover folder could make the next plugin
  import from the UI fail with "Failed to back up existing plugin: Permission
  denied".

### Tests

- 85 new checks (272 total): avoid-rule matching (shows, seasons, movie copies,
  other titles), ranking placement against DV-avoidance, quality and explicit
  picks, recent-play recording, adding/removing rules, popup length, the alert's
  conditions and de-duplication, the notification sender, and the played-shows
  record (latest vs furthest episode, pruning, repeat plays, failures).

## [1.4.0] - 2026-11-27

### Added

- **Avoid low-bitrate copies** — a new opt-in setting (default off). Within one
  video quality tier, a copy whose video bitrate is below that tier's minimum is
  ranked after the healthy copies (ahead of the audio tiebreak), and after audio a
  clearly higher bitrate breaks any remaining tie (bitrates within about 1.5× of
  each other tie, so copies of the same release keep their account-priority
  order). It never moves a copy across tiers, never removes a copy from failover,
  and never demotes a copy whose bitrate is unknown. Takes effect only when
  **Prefer quality** is set.
  - Bitrates are compared as **H.264-equivalent** (HEVC/AV1 ×1.5, VP9 ×1.3,
    MPEG-2 ×0.6, MPEG-4 ×0.8).
  - The video bitrate is read from the video track's own figure (its `BPS` tag or
    `bit_rate`) when present, otherwise from the file's total minus its audio
    track, otherwise the total alone. Totals are stored in kbps by providers and
    in bps by ffprobe-based enrichment; both are handled.
  - New **Minimum bitrate** settings per tier, defaulting to 4K 6 / 1080p 1.5 /
    720p 0.8 / 480p 0.4 Mbps (roughly the bottom 5% of each tier in a large
    real-world library). `0` turns a tier's minimum off.
- **Category quality tag** as a quality signal (Dispatcharr 0.33+). A quality you
  assign to a provider's VOD category (`4K`/`1080p`/`720p`/`480p`/`SD`) now counts
  in the quality waterfall, below the stream name and above the account name.
  Episodes use their series' category. While no category carries a tag the
  plugin doesn't query categories at all, so untagged setups rank exactly as
  before; older Dispatcharr versions simply skip this rung.
- `tools/wanted_survey.sh` — a read-only database report (no provider calls) of
  info coverage and video bitrates for the shows on the VOD Episode Sweep
  watchlist (or every episode), including how many episodes a given set of
  minimums would change. Not shipped in the plugin zip.

### Changed

- **Check status** now reports how many stream-serving workers have the patch
  active. Since Dispatcharr 0.34 actions run in a background worker that never
  serves streams, so the old "active in this worker" line described the wrong
  process. Each process now records its install state in a small Redis hash at
  install/uninstall (stale entries are pruned), and Status reads it.
- The per-selection DEBUG line now includes the chosen stream's `bitrate`.
- `tools/rank_dryrun.sh` shows each candidate's bitrate, whether it counts as
  low-bitrate, and its category quality tag, and ranks with your bitrate
  settings.

### Docs

- Updated the **Prefer quality** help text and README: movie quality selection is
  precise wherever real dimensions exist (for example after a probing plugin has
  enriched them) or a category is tagged; the old "movies are effectively Off or
  prefer-4K" caveat now applies only to movies with neither.

### Tests

- 58 new checks (187 total): bitrate reading in both storage units, codec credit,
  starved demotion and its placement, the bitrate tiebreak, the per-tier minimums,
  category tags (including the untagged and unavailable cases), and the Status
  worker registry.

## [1.3.3] - 2026-09-15

No functional change to selection logic — hardening + docs.

### Changed

- **Signature-agnostic hook.** The `_get_content_and_relation` wrapper now
  forwards any extra positional/keyword arguments straight through to the
  original at every call site. This defends against a future Dispatcharr release
  adding a parameter to that function: a fixed signature would raise `TypeError`
  while binding arguments — before the wrapper's fail-open guard — turning a
  parameter addition into an HTTP 500 on every VOD request. (The wrapper only
  reorders the candidate list core returns, so forwarding is also sufficient for
  correctness — there's no restriction it could undo.)
- **Logger level.** The plugin's logger now adopts the `apps` logger's effective
  level (guarded so anything that already set a level keeps control), so its lines
  survive if the code ever runs in a Celery prefork child (where the root logger
  is left at WARNING). No effect on the request path it hooks today.

### Docs

- Clarified in the **Prefer quality** help text and README that quality tiering is
  precise on TV (measured resolution) but, on movies, depends on name/account
  tokens — so where providers label only 4K, any tier tends to serve the
  4K-labeled copy and can't select an unlabeled 1080p/720p.

### Tests

- Added a structural signature-parity test (unknown args accepted **and**
  forwarded, on both the active and inactive paths); `test_logic.py` now 129
  checks.

## [1.3.2] - 2026-08-11

### Added

- **Docs link** in the plugin card — `help_url` now points at the GitHub repo.
- `tools/rank_dryrun.sh` — a diagnostic that prints the plugin's deterministic
  ranking/decision for any episode from the current database, without changing log
  levels (loads the installed `patch.py` and runs its real ranking logic). Not
  shipped in the plugin zip. README troubleshooting section documents it.

### Changed

- Actions-tab button styling for clearer intent: **Clear one** is now solid blue
  (an action, vs the read-only outline buttons), and **Clear all** is light red to
  signal it's destructive. (The previous `default` variant rendered neutral and
  ignored the `red` color; `light` shows the red tint.)

## [1.3.1] - 2026-08-11

Cosmetic / UI polish only — no behavior change.

### Changed

- Shortened the plugin's display name in the Dispatcharr UI to **VOD Preferences**
  ("Dispatcharr" was redundant inside the Dispatcharr plugin list). The repo,
  folder name, and config key are unchanged, so settings and saved picks are
  unaffected.
- Streamlined the settings help text to the gist; fuller detail now lives in the
  README.

### Fixed

- The **Clear one** button on the Actions tab no longer wraps onto its own line —
  its description was shortened so the button stays right-aligned like the other
  actions.

## [1.3.0] - 2026-08-11

### Added

- **Avoid Dolby Vision without HDR/SDR fallback** — a new opt-in setting (default
  off). Some Dolby Vision streams (Profile 5) carry no HDR10/SDR base layer and
  render with a green/purple cast on players that can't decode DV. When enabled,
  such streams are demoted **below every compatible stream** (the top sort key),
  so the proxy serves a playable copy instead — a compatible 4K when another
  provider has one, otherwise the best compatible lower-resolution copy. A
  no-fallback DV stream is only ever served when it's the sole option (fall back
  to a manual UI pick there). Detection is positive-only — a stream is flagged
  only when its probe carries a Dolby Vision configuration record with no
  base-layer compatibility (`dv_bl_signal_compatibility_id == 0`, i.e. Profile 5),
  so untagged streams are never touched.
- The per-selection DEBUG log line now includes `dv_nofallback=<bool>` for the
  chosen stream, and **Check status** reports `avoid_dv_no_fallback`.

### Notes

- Composes with Prefer 4K and the audio tiebreak (avoidance is the top key,
  resolution next, audio last) and also works with quality set to Off (a pure
  safety demotion). Depends on the same probe metadata as the video/audio signals
  — populated by an advanced/detailed refresh — so it's dormant where absent.

## [1.2.0] - 2026-08-11

### Added

- **Prefer better audio (tiebreaker)** — a new opt-in setting (default off) that
  breaks ties by audio format *among streams of the same video quality tier*. It
  never overrides video quality (a higher-resolution stream always wins) and only
  takes effect when **Prefer quality** is set. Ordering is surround-first, then
  lossless → Dolby → AAC: 5.1/7.1 lossless (TrueHD/DTS) → 5.1/7.1 Dolby (AC3/EAC3)
  → 5.1/7.1 AAC → 5.1/7.1 other → 2.0 lossless → 2.0 Dolby → 2.0 AAC → 2.0 other;
  streams with no readable audio metadata keep their native order. When no video
  signal exists but audio does, audio can decide the pick instead of falling
  through to native.
- The per-selection DEBUG log line now includes the chosen stream's audio
  (`audio=<codec>/<n>ch`), and **Check status** reports `prefer_audio`.

### Notes

- Audio ranking reads per-stream `codec_name` / `channels` from the same probe
  metadata the video-tier waterfall uses (`detailed_info` / `info.info`), which is
  populated by an advanced/detailed refresh — so the tiebreak is dormant wherever
  that metadata is absent.

## [1.1.0] - 2026-08-09

### Added

- **Prefer 1080p** and **Prefer 720p** quality options alongside Prefer 4K. Each
  targets its own tier when available, then steps down to lower tiers before up to
  higher ones — so a bandwidth-minded choice never silently pulls a large 4K
  stream. (The underlying tier ladder already existed; this exposes it in the
  dropdown.) These take effect only where a resolution signal exists — real video
  dimensions, or a provider that labels the tier — and otherwise stay dormant.

### Fixed

- Quality classification from measured video dimensions now allows a small
  tolerance (5%) below each standard **width**, so a cropped 2.39:1
  "cinemascope" 1080p master (commonly 1918/1912 px wide, just under the 1920
  cutoff) is correctly read as 1080p instead of 720p. Standard heights stay
  strict, and the tolerance can only promote a borderline stream to a higher tier
  — never demote one — because adjacent standard widths are far apart.

## [1.0.0] - 2026-08-04

Initial release.

### Added

- **Prefer 4K** — re-orders a VOD title's provider streams by quality before the
  proxy serves it, so the 4K copy wins even when a lower-quality provider has
  higher account priority. Streams with no quality signal keep their native
  account-priority order.
- **Remember my UI pick** — when you play a specific stream from the Dispatcharr
  UI, it's saved as that title's default so the proxy serves the same choice next
  time. Movie picks are remembered at the **stream** level; series picks at the
  **provider** level (quality within the provider is chosen by Prefer 4K).
- Settings (`Prefer quality`: Off / Prefer 4K; `Remember my UI pick`) and actions
  (`Check status`, `List saved picks`, `Clear one`, `Clear all`).
- `test_logic.py` — self-contained logic suite (83 checks) that needs no
  Dispatcharr or database.

### Design notes

- Implemented as a single reload-safe monkeypatch of
  `views._get_content_and_relation` — the one function reached on both the UI
  `/proxy/vod/…?stream_id=…` capture path and the Xtream-Codes apply path that
  clients play through.
- Quality-signal waterfall: **measured video pixel dimensions** (attached cover
  images excluded) → explicit `quality`/`resolution` → provider stream name →
  provider/account name. Actual dimensions are the top signal, so a genuinely
  1080p stream mislabeled "4K" ranks as 1080p.
- Saved picks persist in a dedicated `CoreSettings` row (survive a plugin delete;
  clear them with the **Clear all** action).
- **Fail-open:** if Dispatcharr's internals don't match at install the plugin
  doesn't patch, and any per-request error falls back to native selection.
- Composes with the Dispatcharr VOD Concurrency Fix plugin (disjoint functions).

### Known limitations

- TV saved picks are provider-granular (the Series UI conveys only the account,
  not a specific stream).
- Dispatcharr refreshes TV *episode* streams lazily, not on the normal VOD scan,
  so the plugin can only choose among the streams Dispatcharr currently knows
  about; refresh a series to surface newly-added streams. Movies are unaffected.

[1.5.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.5.0
[1.4.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.4.0
[1.3.3]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.3.3
[1.3.2]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.3.2
[1.3.1]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.3.1
[1.3.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.3.0
[1.2.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.2.0
[1.1.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.1.0
[1.0.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.0.0
