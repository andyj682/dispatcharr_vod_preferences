# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/andyj682/dispatcharr_vod_preferences/releases/tag/v1.0.0
