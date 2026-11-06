"""Tool schemas for the Tightbeam plugin — what the LLM reads."""

STATUS = {
    "name": "tightbeam_status",
    "description": (
        "Tightbeam health check: setup state for both pillars (YouTube sign-in "
        "via `hermes auth add tightbeam:google`, OpusClip via MCP), sweep/audit "
        "progress, and the watch watermark. Run this first when something looks wrong."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

CREDITS = {
    "name": "tightbeam_credits",
    "description": (
        "Check OpusClip credit pools: monthly credits AND processing minutes "
        "(they gate separately). Returns which pool would block a submit."
    ),
    "parameters": {"type": "object", "properties": {}},
}

YT_AUDIT = {
    "name": "yt_audit",
    "description": (
        "Audit the whole channel catalog (read-only, ~1 quota unit per page): "
        "classify shorts/longs/lives, bucket by missing tags/description, "
        "store results in plugin state. Safe to re-run; refreshes the audit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "max_videos": {
                "type": "integer",
                "description": "Cap how many uploads to scan (default: all).",
            },
        },
    },
}

YT_WATCH = {
    "name": "yt_watch",
    "description": (
        "List uploads published since the last watch run (new-video detector "
        "for the post-publish optimizer). Updates the watched watermark."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lookback_hours": {
                "type": "integer",
                "description": "Override the watermark and look back N hours instead.",
            },
        },
    },
}

YT_OPTIMIZE = {
    "name": "yt_optimize",
    "description": (
        "Optimize ONE video's metadata: generate title candidates with the "
        "host LLM (or VidIQ when connected), pick the best, apply standing "
        "policy defaults (category, license, made-for-kids, recording date, "
        "embeddable), push via videos.update, read back and verify. "
        "videos.update costs 50 quota units."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video_id": {"type": "string", "description": "YouTube video id."},
            "fields": {
                "type": "array",
                "items": {"type": "string", "enum": ["title", "description", "tags", "defaults"]},
                "description": "Which fields to touch. Default: tags + defaults only (safe mode).",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Return the planned changes without pushing.",
            },
        },
        "required": ["video_id"],
    },
}

YT_SWEEP = {
    "name": "yt_sweep",
    "description": (
        "Budgeted batch optimization across the audited catalog: works through "
        "priority buckets, respects a daily quota budget, retries transient "
        "412s with backoff, and is fully resumable via plugin state. "
        "Designed to be run from a nightly cron blueprint."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "budget_updates": {
                "type": "integer",
                "description": "Max videos.update calls this run (50 units each). Default 10.",
            },
            "mode": {
                "type": "string",
                "enum": ["tags", "titles", "both"],
                "description": "tags = rule-based tag fills (no LLM); titles = LLM-scored titles for top-view videos.",
            },
            "dry_run": {"type": "boolean"},
        },
    },
}

PUBLISH_UPLOAD = {
    "name": "publish_upload",
    "description": (
        "Upload a local video to OpusClip (resumable, one process) and return "
        "the upload_id. The upload_id stays valid for days and IS the videoUrl "
        "for publish_submit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local path to the mp4."},
        },
        "required": ["path"],
    },
}

PUBLISH_SUBMIT = {
    "name": "publish_submit",
    "description": (
        "Submit an uploaded video as a passthrough project (no re-clipping, "
        "captions OFF — burned captions stay the only layer) and poll until "
        "the clip is COMPLETE. Returns projectId + clipId + preview_url. "
        "Vision-check the preview before scheduling."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "upload_id": {"type": "string"},
            "title": {"type": "string", "description": "Working title for the project."},
        },
        "required": ["upload_id", "title"],
    },
}

PUBLISH_SCHEDULE = {
    "name": "publish_schedule",
    "description": (
        "Schedule a clip to connected platforms at a UTC time. Runs a ±2h "
        "collision check per account, enforces platform caps (FB title <=255, "
        "X <=280, no '>' in YouTube descriptions), schedules, then verifies "
        "with a read-back of the schedule. Returns per-platform results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "clip_id": {"type": "string"},
            "publish_at": {"type": "string", "description": "ISO 8601 UTC time."},
            "platforms": {
                "type": "array",
                "items": {"type": "string", "enum": ["youtube", "tiktok", "instagram", "facebook", "x", "linkedin"]},
                "description": "Default: all connected platforms.",
            },
            "copy": {
                "type": "object",
                "description": "Optional per-platform copy: {platform: {title, description}}. Missing platforms get LLM-drafted copy within caps.",
            },
        },
        "required": ["project_id", "clip_id", "publish_at"],
    },
}

PUBLISH_STATUS = {
    "name": "publish_status",
    "description": (
        "List scheduled/failed posts in a time window or for a project. "
        "Flags silent per-platform failures (e.g. expired OAuth) with the "
        "exact reconnect action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "start_at": {"type": "string", "description": "ISO 8601 UTC window start."},
            "end_at": {"type": "string", "description": "ISO 8601 UTC window end."},
        },
    },
}

PUBLISH_UNSCHEDULE = {
    "name": "publish_unschedule",
    "description": (
        "Cancel a pending scheduled post by schedule_id. Fails cleanly if the "
        "post already published (check publish_status first)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string"},
        },
        "required": ["schedule_id"],
    },
}

ALL = {
    "tightbeam_status": STATUS,
    "tightbeam_credits": CREDITS,
    "yt_audit": YT_AUDIT,
    "yt_watch": YT_WATCH,
    "yt_optimize": YT_OPTIMIZE,
    "yt_sweep": YT_SWEEP,
    "publish_upload": PUBLISH_UPLOAD,
    "publish_submit": PUBLISH_SUBMIT,
    "publish_schedule": PUBLISH_SCHEDULE,
    "publish_status": PUBLISH_STATUS,
    "publish_unschedule": PUBLISH_UNSCHEDULE,
}
