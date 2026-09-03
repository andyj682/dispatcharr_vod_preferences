# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.3.2]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.3.2
[1.3.1]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.3.1
[1.3.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.3.0
[1.2.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.2.0
[1.1.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.1.0
[1.0.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.0.0
