"""Tightbeam OpusClip engine — dispatched through Hermes-managed MCP.

RULE 11 COMPLIANT: this module never reads, refreshes or stores credentials.
Every OpusClip call goes through ctx.dispatch_tool("mcp__opusclip__<tool>"),
so the user's own Hermes-managed MCP connection (`hermes mcp add opusclip`
+ `hermes mcp login opusclip`) owns the OAuth token. The only direct HTTP is
the video-bytes PUT to the signed upload URL that OpusClip itself returns
(no credentials involved).

Baked-in reliability rules (earned in production across hundreds of posts):
- passthrough submits ALWAYS set enableCaption=false (no double-caption layers)
- poll list_clips on the "clip_id" key (not "id")
- FB titles <=255 chars, X titles <=280, no ">" in YouTube descriptions
- ±2h collision check per schedule, 30-min shifts until clear
- trust the submit rejection over get_usage (the credit report can lie)
- read-back verify every schedule; surface silent per-platform failures
"""

import json
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook", "x", "linkedin")

SETUP_HINT = (
    "OpusClip MCP is not connected in this profile. Setup: "
    "`hermes mcp add opusclip --url https://mcp.opus.pro/mcp --auth oauth` then "
    "`hermes mcp login opusclip` (interactive browser)."
)

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        p: {
            "type": "object",
            "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
            "required": ["title"],
        }
        for p in PLATFORMS
    },
}


class OpusError(RuntimeError):
    pass


# ---------------------------------------------------------------- bridge


def _parse(out) -> dict | list:
    """dispatch_tool returns a string; the MCP tool payload is JSON text."""
    if isinstance(out, (dict, list)):
        return out
    s = str(out or "").strip()
    if not s:
        return {}
    try:
        v = json.loads(s)
        if isinstance(v, (dict, list)):
            return v
        return {"text": s[:4000]}
    except Exception:
        low = s.lower()
        if any(k in low for k in ("connection_required", "not connected", "no such tool", "not found", "unknown tool")):
            raise OpusError(f"{SETUP_HINT} ({s[:150]})")
        if "insufficient credits" in low or "not enough credits" in low:
            raise OpusError(f"CREDITS_REJECTED: {s[:300]}")
        return {"text": s[:4000]}


class OpusBridge:
    """Calls the user's Hermes-managed OpusClip MCP tools via dispatch_tool."""

    def __init__(self, ctx):
        if ctx is None or not hasattr(ctx, "dispatch_tool"):
            raise OpusError(SETUP_HINT + " (no plugin context — dispatch unavailable)")
        self.ctx = ctx

    def call(self, tool: str, arguments: dict):
        name = f"mcp__opusclip__{tool}"
        try:
            out = self.ctx.dispatch_tool(name, arguments)
        except OpusError:
            raise
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if any(k in low for k in ("not found", "unknown tool", "no such tool", "not connected", "connection_required")):
                raise OpusError(f"{SETUP_HINT} ({msg[:150]})")
            raise OpusError(f"{tool}: {msg[:300]}")
        payload = _parse(out)
        if isinstance(payload, dict):
            text = str(payload.get("text", ""))
            low = text.lower()
            if any(k in low for k in ("not found", "unknown tool", "connection_required", "not connected")):
                raise OpusError(f"{SETUP_HINT} ({text[:150]})")
            if "insufficient credits" in low or "not enough credits" in low:
                raise OpusError(f"CREDITS_REJECTED: {text[:300]}")
        return payload


# ---------------------------------------------------------------- helpers


def _find_items(payload):
    """OpusClip responses nest lists inconsistently; find the first list of dicts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            try:
                return _find_items(json.loads(text))
            except Exception:
                pass
        for key in ("accounts", "items", "posts", "clips", "projects", "schedules", "data", "result"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                found = _find_items(v)
                if found is not None:
                    return found
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return None


def _match_platform(item: dict, platform: str) -> bool:
    blob = json.dumps(item).lower()
    aliases = {"x": ('"x"', "twitter"), "youtube": ("youtube",), "tiktok": ("tiktok",),
               "instagram": ("instagram",), "facebook": ("facebook",), "linkedin": ("linkedin",)}
    return any(a in blob for a in aliases.get(platform, (platform,)))


def _get(payload: dict, *keys):
    """Tolerant key lookup across camelCase/snake_case + nested data."""
    for k in keys:
        if isinstance(payload, dict) and payload.get(k) not in (None, ""):
            return payload[k]
    if isinstance(payload, dict):
        d = payload.get("data")
        if isinstance(d, dict):
            for k in keys:
                if d.get(k) not in (None, ""):
                    return d[k]
    return None


# ---------------------------------------------------------------- operations


def credits(b: OpusBridge) -> dict:
    usage = b.call("opusclip_get_usage", {})
    # NOTE: get_usage can lie; a submit rejection is the source of truth.
    return {
        "raw": usage,
        "warning": "If submit rejects with 'insufficient credits', TRUST THE REJECTION over this report.",
    }


def list_accounts(b: OpusBridge) -> dict:
    payload = b.call("opusclip_list_social_accounts", {})
    items = _find_items(payload) or []
    found = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        for p in PLATFORMS:
            if _match_platform(it, p) and p not in found:
                found[p] = it
    return {"connected": sorted(found.keys()), "accounts": [i for i in items if isinstance(i, dict)]}


def upload(b: OpusBridge, path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"file not found: {p}"}
    size_mb = max(1, round(p.stat().st_size / (1024 * 1024)))
    ext = p.suffix.lstrip(".").lower() or "mp4"
    link = b.call("opusclip_create_upload_link", {"extension": ext, "sizeMb": size_mb})
    if not isinstance(link, dict):
        return {"error": "unexpected create_upload_link response", "raw": str(link)[:300]}
    upload_url = _get(link, "upload_url", "uploadUrl")
    upload_id = _get(link, "upload_id", "uploadId")
    if not upload_url or not upload_id:
        return {"error": "no upload_url/upload_id in response", "raw": str(link)[:400]}
    # resumable: POST start -> Location -> PUT binary. Never one-shot PUT (400s).
    # The signed URL carries its own auth; no credentials are attached by us.
    req = urllib.request.Request(
        upload_url, data=b"", method="POST",
        headers={"x-goog-resumable": "start", "Content-Length": "0"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        session_url = r.headers.get("Location")
    if not session_url:
        return {"error": "no Location header from resumable start"}
    with open(p, "rb") as f:
        put = urllib.request.Request(
            session_url, data=f.read(), method="PUT", headers={"Content-Type": "video/mp4"}
        )
        with urllib.request.urlopen(put, timeout=1200) as r:
            status = r.status
    if status not in (200, 201):
        return {"error": f"PUT returned {status}"}
    return {
        "upload_id": upload_id,
        "size_mb": size_mb,
        "hint": "upload_id stays valid for days; pass it to publish_submit. Never hand-copy signed URLs.",
    }


def submit(b: OpusBridge, upload_id: str, title: str, poll_s: int = 240) -> dict:
    try:
        proj = b.call(
            "opusclip_submit_project",
            {
                "videoUrl": upload_id,  # the upload_id IS the videoUrl (cdn_url is rejected)
                "title": title[:120],
                "enableCaption": False,  # MANDATORY: prevents double-caption layers
                "skipCurate": True,
                "aspectRatio": "portrait",
                "sourceLang": "en",
            },
        )
    except OpusError as e:
        if "CREDITS_REJECTED" in str(e):
            # trust the rejection over get_usage
            return {"error": "SUBMIT_REJECTED_CREDITS", "detail": str(e)[:300],
                    "action": "top up OpusClip; the upload_id stays valid, retry submit afterwards"}
        raise
    project_id = _get(proj, "projectId", "project_id") if isinstance(proj, dict) else None
    if not project_id:
        msg = json.dumps(proj)[:400] if isinstance(proj, (dict, list)) else str(proj)[:400]
        if "credit" in msg.lower():
            return {"error": "SUBMIT_REJECTED_CREDITS", "detail": msg,
                    "action": "top up OpusClip; the upload_id stays valid, retry submit afterwards"}
        return {"error": "no projectId in submit response", "raw": msg}
    deadline = time.time() + poll_s
    clip = None
    while time.time() < deadline:
        time.sleep(20)
        try:
            payload = b.call("opusclip_list_clips", {"projectId": project_id})
        except OpusError:
            continue
        for it in _find_items(payload) or []:
            # poll on the clip_id KEY — grepping "id" never matches
            if isinstance(it, dict) and (it.get("clip_id") or it.get("clipId")):
                clip = it
                break
        if clip:
            break
    if not clip:
        return {
            "projectId": project_id,
            "status": "STILL_PROCESSING",
            "hint": "processing can stall platform-side; re-check with publish_status later. The upload is NOT lost.",
        }
    return {
        "projectId": project_id,
        "clipId": clip.get("clip_id") or clip.get("clipId"),
        "preview_url": clip.get("preview_url") or clip.get("previewUrl"),
        "status": "COMPLETE",
        "next": "vision-check the preview (exactly one caption layer, no garbled text) BEFORE publish_schedule",
    }


def _draft_copy(llm, title: str, platforms: list) -> dict:
    if llm is None:
        return {p: {"title": title[:250], "description": title} for p in platforms}
    try:
        res = llm.complete_structured(
            instructions=(
                "Write per-platform social copy for one vertical short video. Conventions: "
                "youtube = search-friendly title + description with hashtags at the bottom; "
                "tiktok = hashtag-heavy single caption; instagram = title + spaced caption with "
                "hashtag block; facebook = conversational body copy, minimal hashtags, title MUST "
                "be <=255 chars; x = tight line-broken post, title MUST be <=280 chars; "
                "linkedin = professional hook + short body. No '>' characters in youtube fields."
            ),
            input=[{"type": "text", "text": f"Video working title: {title}\nPlatforms: {', '.join(platforms)}"}],
            json_schema=COPY_SCHEMA,
            purpose="tightbeam.copy-draft",
            temperature=0.6,
            max_tokens=1200,
        )
        parsed = getattr(res, "parsed", None) or {}
        out = {}
        for p in platforms:
            c = parsed.get(p) or {"title": title[:250]}
            out[p] = {"title": c.get("title", title[:250]), "description": c.get("description", c.get("title", ""))}
        return out
    except Exception:
        return {p: {"title": title[:250], "description": title} for p in platforms}


def _enforce_caps(copy: dict) -> dict:
    for p, c in copy.items():
        t = (c.get("title") or "")[:280]
        if p == "facebook":
            t = t[:255]  # hard cap: FB fails at publish time with (#100) over 255
        c["title"] = t
        if p == "youtube":
            c["description"] = (c.get("description") or "").replace(">", "-")  # YT rejects '>' in descriptions
    return copy


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def schedule(b: OpusBridge, project_id: str, clip_id: str, publish_at: str,
             platforms=None, copy=None, title="", llm=None) -> dict:
    accounts = list_accounts(b)
    connected = accounts["connected"]
    want = [p for p in (platforms or connected) if p in connected]
    if not want:
        return {"error": "no matching connected platforms", "connected": connected, "requested": platforms}
    target = _parse_utc(publish_at)
    # collision check ±2h, shift by 30 min until clear (max ±3h)
    window_start = (target - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = (target + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        existing = [e for e in (_find_items(b.call("opusclip_list_scheduled_posts", {"startAt": window_start, "endAt": window_end})) or []) if isinstance(e, dict)]
    except OpusError:
        existing = []
    shifts = [0, 30, -30, 60, -60, 90, -90, 120, -120, 150, -150, 180, -180]
    chosen = None
    for sh in shifts:
        cand = target + timedelta(minutes=sh)
        clash = any(
            _match_platform(e, p) and e.get("publish_at")
            and abs((_parse_utc(e["publish_at"]) - cand).total_seconds()) < 2700
            for e in existing for p in want
        )
        if not clash:
            chosen = cand
            break
    if chosen is None:
        return {"error": "COLLISION_UNCLEAR", "hint": "every slot in ±3h clashes; pick a different time"}
    final_copy = _enforce_caps(copy or _draft_copy(llm, title or clip_id, want))
    results = {}
    for p in want:
        acct = next((a for a in accounts["accounts"] if _match_platform(a, p)), None)
        if not acct:
            results[p] = {"error": "account not found in list"}
            continue
        args = {
            "projectId": project_id,
            "clipId": clip_id,
            "postAccountId": acct.get("postAccountId") or acct.get("post_account_id") or acct.get("id"),
            "publishAt": chosen.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "title": final_copy[p]["title"],
        }
        sub = acct.get("subAccountId") or acct.get("sub_account_id")
        if isinstance(sub, list):
            sub = sub[0] if sub else None
        if sub:
            args["subAccountId"] = sub
        if final_copy[p].get("description"):
            args["description"] = final_copy[p]["description"]
        try:
            r = b.call("opusclip_schedule_publish", args)
            conflict = r.get("has_conflict", r.get("hasConflict")) if isinstance(r, dict) else None
            results[p] = {"scheduled": True, "has_conflict": conflict}
        except OpusError as e:
            results[p] = {"scheduled": False, "error": str(e)[:250]}
    # read-back verify
    time.sleep(3)
    try:
        verify = _find_items(b.call("opusclip_list_scheduled_posts", {"projectId": project_id})) or []
    except OpusError:
        verify = []
    ok = sum(1 for v in results.values() if v.get("scheduled"))
    return {
        "publish_at_utc": chosen.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scheduled": ok,
        "failed": len(results) - ok,
        "per_platform": results,
        "verified_entries": len(verify),
        "copy_used": {p: {"title": c2["title"][:80]} for p, c2 in final_copy.items()},
    }


def status(b: OpusBridge, project_id: str = "", start_at: str = "", end_at: str = "") -> dict:
    args = {}
    if project_id:
        args["projectId"] = project_id
    if start_at and end_at:
        args["startAt"], args["endAt"] = start_at, end_at
    items = [e for e in (_find_items(b.call("opusclip_list_scheduled_posts", args)) or []) if isinstance(e, dict)]
    failed = []
    for e in items:
        if str(e.get("status", "")).lower() == "failed":
            reason = str(e.get("failure_reason") or e.get("failureReason") or "")
            fix = None
            low = reason.lower()
            if "expired" in low:
                fix = "platform OAuth expired: reconnect the account at opus.pro, then re-register this post"
            elif "revoked" in low:
                fix = "connection REVOKED: reconnect the account at opus.pro (Settings > Social Accounts), then re-register pending posts for this platform"
            elif "100" in reason and "title" in low:
                fix = "FB title >255 chars: reschedule with shorter title"
            failed.append({"entry": e, "reason": reason, "fix": fix})
    return {"count": len(items), "posts": items[:50], "failed": failed}


def unschedule(b: OpusBridge, schedule_id: str) -> dict:
    try:
        r = b.call("opusclip_unschedule_publish", {"scheduleId": schedule_id})
        return {"unscheduled": True, "schedule_id": schedule_id, "raw": str(r)[:300]}
    except OpusError as e:
        msg = str(e)
        if "already completed" in msg.lower():
            return {"error": "ALREADY_PUBLISHED",
                    "hint": "check `date` vs publish_at; remediation is per-platform (YT descriptions editable via Data API; X/TikTok/IG/FB posts are not editable via API)"}
        return {"error": msg[:300]}
