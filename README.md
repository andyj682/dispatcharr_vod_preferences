# Dispatcharr VOD Preferences (plugin)

Plugin for Dispatcharr that provides greater control over **which VOD stream** it
serves through its proxy for a given title — control not currently exposed to
clients. It adds two independent, composable features:

1. **Prefer 4K** — re-order a title's provider streams by quality before serving,
   so the 4K copy wins even if a lower-quality provider has higher account
   priority. Stream quality is determined by measured video resolution, then the
   stream name, then the provider name, in that order of priority.
2. **Remember my UI pick** — when you play a specific stream from the Dispatcharr
   UI, save it as that title's default so the proxy serves the same choice next
   time. Useful when the default stream for a title is broken in some non-obvious
   way. Movie choices are saved at the **stream** level; series choices are saved
   at the **provider** level (with finer control available via "Prefer 4K" where
   needed — see below for Dispatcharr's inherent limitations on remembering
   stream-level picks for series).

You still get Dispatcharr's provider slot management and failover; this plugin
only re-orders the candidate list Dispatcharr already built. It works with a
single, reload-safe monkeypatch of `views._get_content_and_relation` — the one
function reached on **both** the UI `/proxy/vod/…?stream_id=…` path (how a pick is
*captured*) and the Xtream-Codes path clients play through (where preferences are
*applied*). See
`DESIGN.md` for the rationale and `patch.py` for the code.

*Targets current Dispatcharr's VOD proxy. If the internals it patches change — or
a request hits an unexpected error — it falls back to native selection (account
priority) rather than breaking playback. No user data leaves the box.*

---

## Install

### Option A — Import via the UI (recommended)
1. Download `dispatcharr_vod_preferences.zip` from the
   [latest release](https://github.com/andyj682/dispatcharr_vod_preferences/releases/latest).
2. Dispatcharr UI → **Plugins** → **Import** → upload the zip.
3. Toggle the plugin **enabled** (accept the trust warning — plugins run
   server-side code).
4. **Restart the Dispatcharr container** (see "Why restart?" below).

### Option B — Drop-in folder (from source)
1. Clone or copy this repo into `data/plugins/dispatcharr_vod_preferences/` on the
   host (→ `/app/data/plugins/…` in the container). The folder must be named
   `dispatcharr_vod_preferences` and contain `plugin.json`, `plugin.py`, and
   `patch.py`. (The folder name matters — the plugin stores its saved picks under
   a key derived from it.)
2. UI → **Plugins** → click **reload** (or `POST /api/plugins/plugins/reload/`).
3. Enable the plugin, then **restart the container**.

### Why restart?
Dispatcharr runs multiple uWSGI workers with `lazy-apps = true`. Each worker
imports an *enabled* plugin's code at boot and applies the monkeypatch then.
Enabling without a restart only reliably patches the worker that handled the
enable request; a restart patches all of them.

---

## Configure

Plugins page → **Dispatcharr VOD Preferences** → **Settings** tab:

- **Prefer quality** — `Off` / `Prefer 4K`. Default `Off`. (Only these two are
  exposed: on real provider data sub-4K streams carry no resolution label, so a
  "Prefer 1080p" option would be misleading. The full tier ladder still lives in
  the code if a provider ever labels sub-4K.)
- **Remember my UI pick** — on/off (default **on**). Governs both capturing new
  picks and applying stored ones.
- **Title key to clear** — a text box used only by the **Clear one** action below.

**Actions** tab:

- **Check status** — is the patch live in this worker, plus current config and
  saved-pick count.
- **List saved picks** — dump the remembered map (`title key → provider/stream`).
- **Clear one** — type a pick's key (from *List saved picks*, e.g. `tmdb:954` or
  `series:…`, or a bare tmdb/imdb id) into the **Title key to clear** box on the
  Settings tab, then click this.
- **Clear all** — wipe every saved pick (quality preference untouched).

---

## How selection works

For every VOD request the plugin calls the native selector, then re-decides,
most-specific-first:

| # | Rule | When it fires |
|---|------|---------------|
| 1 | **Explicit request pick** | The request named a specific stream (`stream_id`, movie UI play) or provider (`m3u_account_id`, series/episode UI play). Passed through untouched; if *Remember my UI pick* is on, it's saved as this title's default. |
| 2 | **Saved UI pick** | A remembered pick for this title. **Movies** pin the exact `(provider, stream)`. **TV pins the provider for the whole series** and still applies the quality rule *within* that provider (so Prefer 4K picks the 4K copy when one provider carries both). Dropped automatically if the provider no longer carries the title. |
| 3 | **Quality rule** | *Prefer quality* is set. Candidates are stable-sorted by quality; the best becomes primary and failover follows quality order. |
| 4 | **Native** | None of the above — untouched account priority. |

Streams with no quality signal keep their native account-priority order (the sort
is stable), so the plugin never reshuffles titles it has no opinion about, and
Dispatcharr's failover still walks the full candidate list.

**Quality signal.** Each stream is ranked by a waterfall of *per-stream* signals.
**Actual video dimensions are the top signal** — ground truth that outranks any
text label, so a stream mislabeled `4K` whose real track is 1920×1080 ranks as
1080p:

1. **Real video pixel dimensions** (movies: `custom_properties.detailed_info`;
   episodes: `info.info`). Attached cover images (PNG/JPEG posters) are skipped so
   a 4K episode's 1920×1080 poster isn't misread as its resolution. Often absent
   until an advanced/detailed refresh has populated it.
2. `custom_properties['quality']` / `['resolution']` — explicit provider label
   (rarely populated on a normal instance).
3. The **provider stream name** (movies: `basic_data.name`; episodes:
   `info.title`) — the reliable signal when dims are absent and the provider puts
   `4K`/`2160p` in the title.
4. The **provider/account name** (`m3u_account.name`) — catches the "separate 4K
   provider" setup (an account named e.g. `… 4K`), and is often the only 4K marker
   for episodes whose titles don't carry it.

Matching is **word-boundary** based, so free-text names don't misfire
(`Wednesday` isn't read as SD, `24K Gold` isn't read as 4K).

> Because the account-name rung exists, avoid putting a bare `HD`/`SD` in an M3U
> account name unless you mean it. `4K`, `1080p`, `720p`, `480p` are the safe,
> unambiguous tokens.

**Movies vs TV — an asymmetry rooted in the UI.** A movie play sends the exact
`stream_id`, so movie picks pin the exact stream. A series play sends only the
`m3u_account_id` (the Series UI has no per-episode stream id), so TV picks are
**provider-granular**: they remember which provider to use for the show, and lean
on Prefer 4K to choose quality *within* that provider. Consequence: to get 4K for
a show whose 4K and non-4K copies live on the **same** account, **Prefer 4K must
be on** — the saved pick supplies the provider, Prefer 4K supplies the quality.

---

## Uninstall / disable

- UI → **Plugins** → toggle **off** (Dispatcharr calls the plugin's `stop()`,
  which reverts the monkeypatch in that worker and deactivates it in the rest).
- For a clean, guaranteed revert across all workers, **restart the container**
  after disabling.
- **Saved picks survive a delete.** They live in their own `CoreSettings` row
  (not the plugin's config), so deleting the plugin leaves them (and reuses them
  if you reinstall). Click **Clear all** first if you want them gone. No changes
  are made to Dispatcharr's own VOD tables.

---

## Troubleshooting

### Verify the patch is live

Each worker logs once at boot / first hit (INFO — always visible):

```
[VOD-PREF] installed VOD preferences patch in worker pid=<PID>
[VOD-PREF] active in worker pid=<PID> at select
```

After enabling + restarting, collect the distinct `pid=` values across a few
plays. You should see **more than one** worker PID. If you only ever see one, the
patch isn't in every worker — restart again and re-check. (The **Check status**
button only reports the one worker that handled that click, so the logs are the
real confirmation.)

### Watch the plugin make decisions (enable DEBUG)

Each selection logs one line — but at **DEBUG** level, so it's quiet by default.
To watch it, set `DISPATCHARR_LOG_LEVEL=DEBUG` in the container's environment and
restart, then:

```bash
docker logs -f <dispatcharr-container> 2>&1 | grep "VOD-PREF"
```

A decision line reads:

```
[VOD-PREF] episode tmdb:12345: quality:4k -> account 7 stream 1978843 (tier=4k, changed=True, candidates=5)
```

- `reason` — which rung fired: `request-pick`, `saved-pick`, `quality:4k`,
  `quality:4k:no-signal`, or nothing logged = `native`.
- `changed=True` — the plugin moved off the native primary (it did real work);
  `changed=False` — its choice already matched native (still confirmation it ran).
- `candidates=N` — how many streams the title has right now (see the episode
  staleness note below if this looks low).

### Test Prefer 4K (the definitive A/B)

`changed=False` proves the rule ran; to prove it can *override* priority, force a
conflict:

1. Temporarily set the 4K provider(s) to a **lower** account priority than the
   non-4K one(s), so native would pick a non-4K stream.
2. **Prefer quality = Off**, play a title with both → serves the non-4K stream.
3. **Prefer quality = Prefer 4K**, play again → serves the 4K stream with
   `changed=True, tier=4k`.
4. Revert the priorities.

### Test Remember my UI pick

1. Play a specific stream (movie) or provider (series) from the Dispatcharr UI →
   **List saved picks** shows a new entry (`tmdb:…` for a movie, `series:…` for a
   show).
2. Play the same title through a client (e.g. Emby) → the log shows `saved-pick`
   and it serves your remembered choice.

### Local logic test

```bash
python test_logic.py
```
Exercises the full ladder, the quality-signal waterfall (dims-first,
cover-image exclusion, name/account fallbacks, word-boundary guards), movie exact
picks, TV show-level picks + same-account 4K composition, capture via
stream_id/account_id, persistence, and clear operations. No Dispatcharr or DB
required.

---

## Limitations / potential fail points

1. **Operates within one title — can't reach an unmerged sibling.** The plugin
   re-orders the streams of a *single* `Movie`/`Episode`. If a provider's 4K copy
   of a show is a **separate, unmerged title** in Dispatcharr (different or absent
   tmdb/imdb, so dedup didn't fuse them), it's on a different content row and will
   never appear in this episode's candidate list. That's a Dispatcharr
   merge/metadata matter (the two need to share a tmdb), not something re-ordering
   can fix. Tell-tale: `candidates=1` with a version you expected missing.

2. **TV episode staleness — the candidate list is only as fresh as Dispatcharr
   made it.** Dispatcharr does **not** refresh episode streams on its own: the VOD
   provider refresh only re-scans *listings*, and per-episode data is fetched
   lazily (via the XC `get_series_info` action / opening the series in the UI),
   gated to once per 24h. So a provider that adds a 4K stream later can be
   invisible — the plugin faithfully serves the best of whatever streams currently
   exist, which may be a stale single 720p. This is the most impactful gotcha for
   the TV side; keep episodes fresh by (a) having your library/`.strm` tooling
   call `get_series_info` for curated series, or (b) scheduling Dispatcharr's
   (otherwise dormant) `batch_refresh_series_episodes` task. Movies are unaffected
   (their streams are part of the listing scan).

3. **TV picks are provider-granular.** The Series UI conveys only the account, not
   a stream, so a saved TV pick can't distinguish two same-account, same-quality
   streams (e.g. one wrong-aspect-ratio 1080p vs another 1080p). Same-account 4K
   vs non-4K is recovered via Prefer 4K; a same-quality distinction isn't
   expressible for TV.

4. **Coverage.** Applies to VOD selection on the proxy path (movies + episodes),
   reached from both the UI and the Xtream-Codes (XC) play path. Live TV, EPG, and
   DVR are not touched. The XC play path never *captures* a pick (its redirect
   carries no `stream_id`/`m3u_account_id`), only *applies* — capture happens on a
   stream-specific UI play.

5. **Fail-open.** If Dispatcharr's internals don't match at install, the plugin
   doesn't patch (native behaviour intact). If the ladder ever errors on a
   request, it falls back to native selection for that request. Disable / delete /
   reload reverts the patch via `stop()`.

---

## Where data is stored

- **Saved picks:** a dedicated `CoreSettings` row, `key =
  dispatcharr_vod_preferences_picks`, value `{ title_key: {…} }`. Kept out of the
  plugin's own settings on purpose — the Plugins UI re-saves the whole settings
  blob before every action, which would otherwise clobber picks written
  server-side. Movie keys: `tmdb:` → `imdb:` → `as:<account>:<stream>`. TV keys
  (per series): `stmdb:` → `simdb:` → `series:<uuid>`. Picks from a pre-1.0
  install migrate across automatically on first write.
- **Settings** (`prefer_quality`, `remember_ui_picks`, `clear_key`): the plugin's
  normal `PluginConfig.settings`.

Dispatcharr's own VOD tables are never modified.

---

## Composes with Dispatcharr VOD Concurrency Fix

Runs cleanly alongside the
[VOD Concurrency Fix](https://github.com/andyj682/dispatcharr_vod_concurrency_fix)
plugin — that one touches `stream_vod` / profile reservation, this one only
re-orders the relation list. Disjoint functions, verified running together. This
plugin decides *which* provider/stream backs a title; the concurrency fix keeps
the resulting playback from failing over across providers mid-burst.

---

## Acknowledgments

Designed and built by [andyj682](https://github.com/andyj682) with Claude
(Anthropic) as a pair-programming collaborator — Dispatcharr code analysis,
selection design, and implementation.
