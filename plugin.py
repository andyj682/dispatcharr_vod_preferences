"""
Dispatcharr VOD Preferences
===========================

Controls which provider stream Dispatcharr serves for a VOD title through its
proxy, where the native behaviour is pure account priority:

  * Prefer a quality tier (e.g. 4K) across providers, and/or
  * Remember the stream you pick in the Dispatcharr UI and reuse it as that
    title's durable default (so the proxy serves the same stream).

See patch.py for the full design writeup and the selection ladder. This module
is the plugin entry point: it applies the monkeypatch at import time (Dispatcharr
imports an enabled plugin's code in every uWSGI worker at boot) and reverts it in
stop().

Author: andyj682
License: MIT
"""

import logging

logger = logging.getLogger("plugins.dispatcharr_vod_preferences")

# Apply the patch as soon as the module is imported. Dispatcharr only imports an
# enabled plugin's code, and under `lazy-apps = true` every uWSGI worker imports
# it at boot -- so importing == "this worker should be patched".
try:
    from . import patch as _patch
except Exception:  # pragma: no cover - fall back to flat import layout
    import patch as _patch

try:
    _patch.install()
except Exception:  # never break app startup because of the plugin
    logger.exception("[VOD-PREF] auto-install on import failed")


def _format_picks(picks):
    """Human-readable one-line-per-pick summary for the UI message."""
    if not picks:
        return "No saved picks."
    lines = []
    for key, rec in sorted(picks.items()):
        acct = rec.get("m3u_account_id") if isinstance(rec, dict) else "?"
        if isinstance(rec, dict) and "stream_id" in rec:
            target = f"account {acct}, stream {rec.get('stream_id')}"
        else:
            # Show-level pick: provider for the whole series (stmdb:/simdb:/series:).
            target = f"account {acct} (whole series)"
        lines.append(f"  {key}  ->  {target}")
    return f"{len(picks)} saved pick(s):\n" + "\n".join(lines)


def _format_workers(report):
    """One sentence on where the patch is active, for the Status action.

    Since Dispatcharr 0.34, actions run in a background (Celery) worker that
    never serves streams, so the answer comes from what each stream-serving
    (uWSGI) worker recorded when it installed the patch.
    """
    import os
    if not report.get("available"):
        return (f"Patch active in this process: {_patch._ACTIVE} (pid {os.getpid()}); "
                "the per-worker registry could not be read, so check the logs for "
                "'[VOD-PREF] installed' lines.")
    active, inactive = report.get("active") or [], report.get("inactive") or []
    if not active and not inactive:
        return ("No stream-serving worker has reported in yet. Restart the "
                "container if the plugin was just installed or updated.")
    msg = f"Patch active in {len(active)} stream-serving worker(s)"
    if active:
        msg += " (pids " + ", ".join(str(p) for p in active) + ")"
    if inactive:
        msg += "; NOT active in " + ", ".join(str(p) for p in inactive)
    msg += "."
    if report.get("here") != "uwsgi":
        msg += f" This check ran in a {report.get('here')} process (pid {os.getpid()})."
    return msg


class Plugin:
    name = "VOD Preferences"
    version = "1.5.0"
    description = (
        "Greater control over which VOD stream Dispatcharr serves for a given "
        "title: prefer higher video/audio qualities across providers, remember "
        "preferred streams for specific titles, and more."
    )
    author = "andyj682"
    help_url = "https://github.com/andyj682/dispatcharr_vod_preferences"

    fields = [
        {
            "id": "prefer_quality",
            "label": "Prefer quality",
            "type": "select",
            "default": "off",
            "help_text": (
                "Sets the highest video quality to prefer. Quality is inferred from "
                "actual video pixel dimensions, explicit quality/resolution, provider "
                "stream name, the category's quality tag (if you set one), and M3U "
                "account name (e.g., a '4K' provider), in that order. A chosen tier "
                "wins when available; otherwise lower priorities are tried before "
                "higher ones in descending order. Streams with no quality signal keep "
                "their native account-priority order. Precise wherever measured "
                "resolution exists (most TV, and movies enriched by a probing plugin); "
                "for movies with no measurements, a tagged category or a '4K' label "
                "in the stream name is what tells copies apart."
            ),
            # The full tier ladder lives in patch.py (_QUALITY_PRIORITY). 1080p/720p
            # depend on a resolution signal being present (real dims or a labelled
            # tier); when a provider carries no sub-4K label they stay dormant and
            # behave like Off, which is safe.
            "options": [
                {"value": "off", "label": "Off (native account priority)"},
                {"value": "4k", "label": "Prefer 4K"},
                {"value": "1080p", "label": "Prefer 1080p"},
                {"value": "720p", "label": "Prefer 720p"},
            ],
        },
        {
            "id": "remember_ui_picks",
            "label": "Remember my UI pick",
            "type": "boolean",
            "default": True,
            "help_text": (
                "Save a specific stream played from the Dispatcharr UI as the default "
                "so the proxy serves it next time. Movies remember the exact stream; "
                "TV remembers the PROVIDER for the whole series. Applied ahead of the "
                "quality rule; dropped automatically if the provider stops carrying "
                "the title."
            ),
        },
        {
            "id": "prefer_audio",
            "label": "Prefer better audio (tiebreaker)",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Among streams of the same video quality, prefer higher-quality "
                "audio (5.1/7.1 -> 2.0, and within each category lossless -> Dolby "
                "-> AAC -> other). Streams with no readable audio info keep their "
                "native order."
            ),
        },
        {
            "id": "avoid_dv_no_fallback",
            "label": "Avoid Dolby Vision without HDR/SDR fallback",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Demote Dolby Vision streams with no HDR10/SDR base layer (Profile 5) "
                "to lowest priority to avoid playback compatibility issues. Leave off "
                "if your players handle Profile 5 fine."
            ),
        },
        {
            "id": "avoid_low_bitrate",
            "label": "Avoid low-bitrate copies",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Within the same video quality, rank a copy whose video bitrate is "
                "below the minimum for its tier (settings below) after the healthy "
                "copies, and let a clearly higher bitrate break any remaining tie. "
                "Bitrates are compared as H.264-equivalent (HEVC/AV1 count 1.5x). "
                "Never moves a copy across quality tiers; copies with no bitrate "
                "info are never demoted. Takes effect only when Prefer quality is set."
            ),
        },
        {
            "id": "min_bitrate_4k",
            "label": "Minimum bitrate, 4K (Mbps)",
            "type": "number",
            "default": 6,
            "help_text": "H.264-equivalent video Mbps below which a 4K copy counts as low-bitrate. 0 turns this tier's minimum off. Default 6.",
        },
        {
            "id": "min_bitrate_1080p",
            "label": "Minimum bitrate, 1080p (Mbps)",
            "type": "number",
            "default": 1.5,
            "help_text": "Same, for 1080p. Default 1.5.",
        },
        {
            "id": "min_bitrate_720p",
            "label": "Minimum bitrate, 720p (Mbps)",
            "type": "number",
            "default": 0.8,
            "help_text": "Same, for 720p. Default 0.8.",
        },
        {
            "id": "min_bitrate_480p",
            "label": "Minimum bitrate, 480p/SD (Mbps)",
            "type": "number",
            "default": 0.4,
            "help_text": "Same, for 480p and SD. Default 0.4.",
        },
        {
            "id": "clear_key",
            "label": "Title key to clear",
            "type": "string",
            "default": "",
            "placeholder": "e.g. tmdb:954  (or a bare tmdb/imdb id)",
            "help_text": (
                "Type the key of a saved pick here (copy it from 'List saved "
                "picks'), then click 'Clear one'. Leave blank "
                "otherwise."
            ),
        },
        {
            "id": "avoid_play_number",
            "label": "Recent play # to avoid",
            "type": "number",
            "default": 1,
            "help_text": (
                "Which entry from 'List recent plays' the 'Avoid copy' button marks "
                "as a bad copy (1 = the most recent). TV: avoids that provider for the "
                "show. Movies: avoids that exact copy. An avoided copy is served "
                "only if nothing else can play."
            ),
        },
        {
            "id": "avoid_season_only",
            "label": "Avoid just that season (TV)",
            "type": "boolean",
            "default": False,
            "help_text": (
                "When on, a TV avoid rule covers only the season of the episode you "
                "picked instead of the whole show. Ignored for movies."
            ),
        },
        {
            "id": "avoid_rule_to_remove",
            "label": "Avoid rule # to remove",
            "type": "number",
            "default": 0,
            "help_text": "The number shown by 'List avoid rules', for the 'Remove rule' button.",
        },
        {
            "id": "notify_missed_4k",
            "label": "Notify on missed 4K",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Send an alert when a title plays below 4K because its only 4K "
                "copy is Dolby Vision without an HDR/SDR fallback. Needs Prefer 4K "
                "and the Dolby Vision setting on, plus a Prowl key or webhook "
                "below. At most one alert per title per day."
            ),
        },
        {
            "id": "prowl_api_key",
            "label": "Prowl API key",
            "type": "string",
            "input_type": "password",
            "default": "",
            "help_text": "Optional. Your Prowl API key (prowlapp.com).",
        },
        {
            "id": "webhook_url",
            "label": "Webhook URL",
            "type": "string",
            "default": "",
            "placeholder": "https://...",
            "help_text": (
                "Optional. Receives a JSON POST with title, message, text and "
                "content fields (works with Slack/Discord-style webhooks, Home "
                "Assistant and similar)."
            ),
        },
        {
            "id": "_info",
            "label": "",
            "type": "info",
            "description": (
                "Selection order (most specific first): explicit request pick -> "
                "saved UI pick -> avoid DV without fallback -> avoid rules -> "
                "quality tier -> avoid low bitrate -> audio -> bitrate -> native "
                "account priority. Use the buttons below to inspect or clear saved "
                "picks and avoid rules."
            ),
        },
    ]

    actions = [
        {
            "id": "status",
            "label": "Show patch status",
            "description": "Report whether the preferences patch is active in "
                           "the workers that serve streams, plus current settings.",
            "button_label": "Check status",
            "button_variant": "outline",
        },
        {
            "id": "list_saved",
            "label": "List saved picks",
            "description": "Show the remembered per-title stream picks.",
            "button_label": "List saved picks",
            "button_variant": "outline",
        },
        {
            "id": "clear_title",
            "label": "Clear one saved pick",
            "description": "Remove the saved pick whose key is in the "
                           "'Title key to clear' box.",
            "button_label": "Clear one",
            "button_variant": "filled",
        },
        {
            "id": "clear_saved",
            "label": "Clear all saved picks",
            "description": "Remove every remembered per-title stream pick.",
            "button_label": "Clear all",
            "button_variant": "light",
            "button_color": "red",
            "confirm": {
                "title": "Clear all saved picks?",
                "message": "This permanently removes every remembered per-title "
                           "stream pick. Quality preferences are unaffected.",
            },
        },
        {
            "id": "list_recent",
            "label": "List recent plays",
            "description": "Show the last 5 titles the proxy served, with the "
                           "provider each one played from.",
            "button_label": "List recent plays",
            "button_variant": "outline",
        },
        {
            "id": "avoid_recent",
            "label": "Avoid a recent play",
            "description": "Mark the copy from 'Recent play # to avoid' as bad, "
                           "so it is served only as a last resort.",
            "button_label": "Avoid copy",
            "button_variant": "filled",
        },
        {
            "id": "list_avoid",
            "label": "List avoid rules",
            "description": "Show the copies and providers you've marked to avoid.",
            "button_label": "List avoid rules",
            "button_variant": "outline",
        },
        {
            "id": "remove_avoid",
            "label": "Remove an avoid rule",
            "description": "Remove the rule numbered in 'Avoid rule # to remove'.",
            "button_label": "Remove rule",
            "button_variant": "filled",
        },
        {
            "id": "clear_avoid",
            "label": "Clear all avoid rules",
            "description": "Remove every avoid rule.",
            "button_label": "Clear all",
            "button_variant": "light",
            "button_color": "red",
            "confirm": {
                "title": "Clear all avoid rules?",
                "message": "This permanently removes every avoid rule. Saved picks "
                           "and quality preferences are unaffected.",
            },
        },
        {
            "id": "test_notify",
            "label": "Send test notification",
            "description": "Send a test alert to the Prowl key and/or webhook in "
                           "Settings.",
            "button_label": "Send test",
            "button_variant": "outline",
        },
    ]

    def run(self, action=None, params=None, context=None):
        params = params or {}
        context = context or {}

        if action == "enable":
            ok = _patch.install()
            return {
                "status": "ok" if ok else "error",
                "message": "VOD preferences patch installed"
                if ok else "Failed to install (see logs)",
            }

        if action == "disable":
            _patch.uninstall()
            return {"status": "ok", "message": "VOD preferences patch reverted"}

        if action == "status":
            settings = context.get("settings", {})
            floors = _patch._parse_floors(settings)
            floor_txt = "/".join(
                f"{t}={floors[t]:g}" for t in ("4k", "1080p", "720p", "480p") if t in floors
            ) or "none"
            return {
                "status": "ok",
                "message": (
                    _format_workers(_patch.worker_report()) + " "
                    f"prefer_quality={settings.get('prefer_quality', 'off')}, "
                    f"remember_ui_picks={settings.get('remember_ui_picks', True)}, "
                    f"prefer_audio={settings.get('prefer_audio', False)}, "
                    f"avoid_dv_no_fallback={settings.get('avoid_dv_no_fallback', False)}, "
                    f"avoid_low_bitrate={settings.get('avoid_low_bitrate', False)} "
                    f"(min Mbps {floor_txt}), "
                    f"saved_picks={len(_patch.get_saved_picks())}, "
                    f"avoid_rules={len(_patch.get_avoid_rules())}, "
                    f"played_shows={len(_patch.get_played_series())}, "
                    f"notify_missed_4k={settings.get('notify_missed_4k', False)}"
                    + (" (no Prowl key or webhook set)"
                       if settings.get('notify_missed_4k') and not (
                           str(settings.get('prowl_api_key') or '').strip()
                           or str(settings.get('webhook_url') or '').strip()) else "")
                ),
            }

        if action == "list_saved":
            picks = _patch.get_saved_picks()
            return {"status": "ok", "message": _format_picks(picks), "picks": picks}

        if action == "clear_saved":
            try:
                removed = _patch.clear_all_saved()
                return {"status": "ok", "message": f"Cleared {removed} saved pick(s)."}
            except Exception as exc:
                logger.exception("[VOD-PREF] clear_saved failed")
                return {"status": "error", "message": f"Failed to clear: {exc}"}

        if action == "clear_title":
            # The Plugins UI has no per-action parameter input, but it DOES save
            # settings before running an action -- so the key is read from the
            # 'clear_key' settings field. (params are still honoured if a caller
            # provides them via the API directly.)
            settings = context.get("settings", {})
            key = (
                (settings.get("clear_key") or "").strip()
                or params.get("title_key")
                or params.get("key")
                or params.get("title")
                or params.get("tmdb")
                or params.get("imdb")
                or params.get("value")
            )
            if not key:
                return {
                    "status": "error",
                    "message": "Type a title key (from 'List saved picks') or a "
                               "tmdb/imdb id into the 'Title key to clear' field, "
                               "then click 'Clear one'.",
                }
            try:
                removed = _patch.clear_saved_key(key)
                if removed:
                    return {"status": "ok", "message": f"Removed saved pick for '{key}'."}
                return {"status": "ok", "message": f"No saved pick matched '{key}'."}
            except Exception as exc:
                logger.exception("[VOD-PREF] clear_title failed")
                return {"status": "error", "message": f"Failed to clear: {exc}"}

        if action == "list_recent":
            try:
                rows = _patch.describe_recent_plays()
            except Exception as exc:
                logger.exception("[VOD-PREF] list_recent failed")
                return {"status": "error", "message": f"Failed to list recent plays: {exc}"}
            return {
                "status": "ok",
                "message": _patch.format_recent_plays(rows),
                "plays": [{k: r[k] for k in ("n", "label", "account", "ago")} for r in rows],
            }

        if action == "avoid_recent":
            settings = context.get("settings", {})
            number = settings.get("avoid_play_number", params.get("play", 1))
            season_only = _patch._as_bool(settings.get("avoid_season_only", False), False)
            try:
                res = _patch.add_avoid_from_recent(number, season_only)
            except Exception as exc:
                logger.exception("[VOD-PREF] avoid_recent failed")
                return {"status": "error", "message": f"Failed to add avoid rule: {exc}"}
            return {"status": "ok" if res["ok"] else "error", "message": res["message"]}

        if action == "list_avoid":
            rules = _patch.get_avoid_rules()
            return {"status": "ok", "message": _patch.format_avoid_rules(rules), "rules": rules}

        if action == "remove_avoid":
            settings = context.get("settings", {})
            number = settings.get("avoid_rule_to_remove", params.get("rule", 0))
            try:
                res = _patch.remove_avoid_rule(number)
            except Exception as exc:
                logger.exception("[VOD-PREF] remove_avoid failed")
                return {"status": "error", "message": f"Failed to remove avoid rule: {exc}"}
            return {"status": "ok" if res["ok"] else "error", "message": res["message"]}

        if action == "clear_avoid":
            try:
                removed = _patch.clear_all_avoid()
                return {"status": "ok", "message": f"Cleared {removed} avoid rule(s)."}
            except Exception as exc:
                logger.exception("[VOD-PREF] clear_avoid failed")
                return {"status": "error", "message": f"Failed to clear: {exc}"}

        if action == "test_notify":
            settings = context.get("settings", {})
            prowl = str(settings.get("prowl_api_key") or "").strip()
            hook = str(settings.get("webhook_url") or "").strip()
            if not prowl and not hook:
                return {"status": "error",
                        "message": "Enter a Prowl API key and/or a webhook URL first."}
            results = _patch.send_notification(
                prowl, hook, "VOD Preferences test",
                "Test alert from VOD Preferences. Missed-4K alerts will look like this.")
            summary = ", ".join(f"{ch}: {'sent' if ok else 'FAILED'} ({detail})"
                                for ch, ok, detail in results)
            all_ok = bool(results) and all(ok for _c, ok, _d in results)
            return {"status": "ok" if all_ok else "error", "message": summary}

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context=None):
        """Called by Dispatcharr on disable / delete / reload."""
        _patch.uninstall()
        return {"status": "ok", "message": "VOD preferences patch reverted"}
