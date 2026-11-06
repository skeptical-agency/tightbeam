"""Tool handlers for the Tightbeam plugin.

Rules: every handler takes (args: dict, **kwargs), never raises, and always
returns a JSON string. Heavy deps (google-api-python-client) are imported
lazily inside handlers so the plugin loads without them.
"""

import json
import os
from pathlib import Path

# Set by register() in __init__.py — gives handlers access to ctx.llm etc.
_CTX = None


def bind_ctx(ctx):
    global _CTX
    _CTX = ctx


def _data_dir() -> Path:
    """Plugin state dir that survives updates and follows the active profile."""
    try:
        from plugins.plugin_storage import plugin_data_dir  # documented public API

        return Path(plugin_data_dir("tightbeam"))
    except Exception:
        home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        p = Path(home) / "plugin-data" / "tightbeam"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra})


def _not_implemented(tool: str) -> str:
    return _err(
        "not_implemented",
        tool=tool,
        hint="Tightbeam scaffold v0.1.0 — this tool lands in a later build. See README.",
    )


# ---------------------------------------------------------------- status


def tightbeam_status(args: dict, **kwargs) -> str:
    d = _data_dir()
    yt_token = d / "youtube_token.json"
    yt_client = d / "client_secret.json"
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    opus_tokens = (hermes_home / "mcp-tokens" / "opusclip.json").exists()
    state: dict = {}
    sf = d / "state.json"
    if sf.exists():
        try:
            loaded = json.loads(sf.read_text())
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {"error": "state.json corrupt"}
    def _dict(key):
        v = state.get(key)
        return v if isinstance(v, dict) else {}

    audit = _dict("audit")
    channel = _dict("channel")
    sweep_st = _dict("sweep")
    watch_st = _dict("watch")
    return json.dumps(
        {
            "plugin": "tightbeam",
            "version": "0.3.0",
            "data_dir": str(d),
            "youtube": {
                "client_secret": yt_client.exists(),
                "token": yt_token.exists(),
                "ready": yt_token.exists(),
                "channel": channel.get("title"),
                "hint": None if yt_token.exists() else "run yt_auth_url (see tightbeam skill for GCP setup)",
            },
            "opusclip": {
                "token_found": opus_tokens,
                "hint": None if opus_tokens else "run: hermes mcp login opusclip",
            },
            "sweep": {
                "audited_videos": len(audit.get("videos", [])),
                "audited_at": audit.get("ts"),
                "swept_videos": len(sweep_st.get("done", [])),
            },
            "watch": {"watermark": watch_st.get("watermark")},
        }
    )


# ---------------------------------------------------------------- youtube


def _youtube_mod():
    try:
        from . import youtube  # plugin package context
    except ImportError:
        import youtube  # dev/test context
    return youtube


def _safe(fn, *a, **kw) -> str:
    try:
        return json.dumps(fn(*a, **kw), default=str)
    except Exception as e:  # noqa: BLE001 — handlers never raise
        return _err(str(e)[:400], type=type(e).__name__)


def yt_auth_url(args: dict, **kwargs) -> str:
    y = _youtube_mod()
    return _safe(y.auth_url, args.get("client_secret_file", ""))


def yt_auth_code(args: dict, **kwargs) -> str:
    y = _youtube_mod()
    return _safe(y.auth_code, args.get("code", ""))


def yt_audit(args: dict, **kwargs) -> str:
    y = _youtube_mod()
    return _safe(y.audit, int(args.get("max_videos") or 0))


def yt_watch(args: dict, **kwargs) -> str:
    y = _youtube_mod()
    return _safe(y.watch, int(args.get("lookback_hours") or 0))


def yt_optimize(args: dict, **kwargs) -> str:
    y = _youtube_mod()
    llm = getattr(_CTX, "llm", None) if _CTX is not None else None
    return _safe(
        y.optimize,
        args.get("video_id", ""),
        args.get("fields"),
        bool(args.get("dry_run")),
        llm,
        _CTX,
    )


def yt_sweep(args: dict, **kwargs) -> str:
    y = _youtube_mod()
    llm = getattr(_CTX, "llm", None) if _CTX is not None else None
    return _safe(
        y.sweep,
        int(args.get("budget_updates") or 10),
        args.get("mode") or "both",
        bool(args.get("dry_run")),
        llm,
        _CTX,
    )


# ---------------------------------------------------------------- publish


def _opus_mod():
    try:
        from . import opusclip  # plugin package context
    except ImportError:
        import opusclip  # dev/test context
    return opusclip


def tightbeam_credits(args: dict, **kwargs) -> str:
    o = _opus_mod()
    return _safe(o.credits)


def publish_upload(args: dict, **kwargs) -> str:
    o = _opus_mod()
    return _safe(o.upload, args.get("path", ""))


def publish_submit(args: dict, **kwargs) -> str:
    o = _opus_mod()
    return _safe(o.submit, args.get("upload_id", ""), args.get("title", "short"))


def publish_schedule(args: dict, **kwargs) -> str:
    o = _opus_mod()
    llm = getattr(_CTX, "llm", None) if _CTX is not None else None
    return _safe(
        o.schedule,
        args.get("project_id", ""),
        args.get("clip_id", ""),
        args.get("publish_at", ""),
        args.get("platforms"),
        args.get("copy"),
        (args.get("copy") or {}).get("youtube", {}).get("title", "") if isinstance(args.get("copy"), dict) else "",
        llm,
    )


def publish_status(args: dict, **kwargs) -> str:
    o = _opus_mod()
    return _safe(o.status, args.get("project_id", ""), args.get("start_at", ""), args.get("end_at", ""))


def publish_unschedule(args: dict, **kwargs) -> str:
    o = _opus_mod()
    return _safe(o.unschedule, args.get("schedule_id", ""))


HANDLERS = {
    "tightbeam_status": tightbeam_status,
    "tightbeam_credits": tightbeam_credits,
    "yt_auth_url": yt_auth_url,
    "yt_auth_code": yt_auth_code,
    "yt_audit": yt_audit,
    "yt_watch": yt_watch,
    "yt_optimize": yt_optimize,
    "yt_sweep": yt_sweep,
    "publish_upload": publish_upload,
    "publish_submit": publish_submit,
    "publish_schedule": publish_schedule,
    "publish_status": publish_status,
    "publish_unschedule": publish_unschedule,
}
