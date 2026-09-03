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


class Plugin:
    name = "VOD Preferences"
    version = "1.3.2"
    description = (
        "Greater control over which VOD stream Dispatcharr serves through its "
        "proxy for a given title: prefer higher video/audio qualities across "
        "providers, and/or remember a stream selected in the UI as that title's "
        "durable default."
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
                "stream name, and M3U account name (e.g., a '4K' provider), in that "
                "order. A chosen tier wins when available; otherwise lower priorities "
                "are tried before higher ones in descending order. Streams with no "
                "quality signal keep their native account-priority order."
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
            "id": "clear_key",
            "label": "Title key to clear",
            "type": "string",
            "default": "",
            "placeholder": "e.g. tmdb:954  (or a bare tmdb/imdb id)",
            "help_text": (
                "Type the key of a saved pick here (copy it from 'List saved "
                "picks'), then click 'Clear one' on the Actions tab. Leave blank "
                "otherwise."
            ),
        },
        {
            "id": "_info",
            "label": "",
            "type": "info",
            "description": (
                "Selection order (most specific first): explicit request pick -> "
                "saved UI pick -> quality rule -> native account priority. Use the "
                "buttons below to inspect or clear saved picks."
            ),
        },
    ]

    actions = [
        {
            "id": "status",
            "label": "Show patch status",
            "description": "Report whether the preferences patch is active in the "
                           "worker that handles this request.",
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
                           "'Title key to clear' box (Settings tab).",
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
            import os
            settings = context.get("settings", {})
            return {
                "status": "ok",
                "message": (
                    f"active={_patch._ACTIVE} in worker pid={os.getpid()} "
                    f"(reflects ONE worker; check logs for all worker pids). "
                    f"prefer_quality={settings.get('prefer_quality', 'off')}, "
                    f"remember_ui_picks={settings.get('remember_ui_picks', True)}, "
                    f"prefer_audio={settings.get('prefer_audio', False)}, "
                    f"avoid_dv_no_fallback={settings.get('avoid_dv_no_fallback', False)}, "
                    f"saved_picks={len(_patch.get_saved_picks())}"
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

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context=None):
        """Called by Dispatcharr on disable / delete / reload."""
        _patch.uninstall()
        return {"status": "ok", "message": "VOD preferences patch reverted"}
