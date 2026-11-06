"""Tightbeam OpusClip engine — direct MCP streamable-HTTP client.

Works WITHOUT in-session MCP tools (cron-safe): talks to https://mcp.opus.pro/mcp
directly using the OAuth token that `hermes mcp login opusclip` stores under
<HERMES_HOME>/mcp-tokens/. Token refresh handled in-process.

Baked-in reliability rules (earned in production across hundreds of posts):
- passthrough submits ALWAYS set enableCaption=false (no double-caption layers)
- poll list_clips on the "clip_id" key (not "id")
- FB titles <=255 chars, X titles <=280, no ">" in YouTube descriptions
- ±2h collision check per schedule, 30-min shifts until clear
- trust the submit rejection over get_usage (the credit report can lie)
- read-back verify every schedule; surface silent per-platform failures
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENDPOINT = "https://mcp.opus.pro/mcp"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
}

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook", "x", "linkedin")

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


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _tok_path() -> Path:
    return _hermes_home() / "mcp-tokens" / "opusclip.json"


def _cli_path() -> Path:
    return _hermes_home() / "mcp-tokens" / "opusclip.client.json"


# ---------------------------------------------------------------- client


class OpusError(RuntimeError):
    pass


class OpusClient:
    def __init__(self):
        if not _tok_path().exists() or not _cli_path().exists():
            raise OpusError(
                "OPUSCLIP_NOT_AUTHENTICATED: no token under <HERMES_HOME>/mcp-tokens/. "
                "Run `hermes mcp login opusclip` interactively (needs a browser)."
            )
        self.tok = json.loads(_tok_path().read_text())
        self.cli = json.loads(_cli_path().read_text())
        if time.time() > self.tok.get("expires_at", 0) - 30:
            self._refresh()
        self.sid = None
        self._init_session()

    def _refresh(self):
        req = urllib.request.Request(
            self.cli["issuer"].rstrip("/") + "/oauth2/token",
            data=urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.tok["refresh_token"],
                    "client_id": self.cli["client_id"],
                }
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", **UA},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                new = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise OpusError(
                f"token refresh failed (HTTP {e.code}). OpusClip OAuth can die outright; "
                "fix: `hermes mcp login opusclip`"
            )
        new.setdefault("expires_at", time.time() + new.get("expires_in", 300) - 15)
        new.setdefault("refresh_token", self.tok["refresh_token"])
        _tok_path().write_text(json.dumps(new))
        self.tok = new

    def _init_session(self):
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "tightbeam", "version": "0.3.0"},
            },
        }
        req = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.tok['access_token']}",
                **UA,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self.sid = r.headers.get("Mcp-Session-Id")
                r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code in (401, 403):
                raise OpusError(f"OpusClip auth rejected (HTTP {e.code}): {detail}. Fix: `hermes mcp login opusclip`")
            raise OpusError(f"init HTTP {e.code}: {detail}")
        self._raw("notifications/initialized", {}, notif=True)

    def _raw(self, method, params, notif=False):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notif:
            body["id"] = str(uuid.uuid4())
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.tok['access_token']}",
            **UA,
        }
        if self.sid:
            hdrs["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read().decode()
        if notif or not data.strip():
            return None
        for line in data.splitlines():
            if line.startswith("data: "):
                j = json.loads(line[6:])
                if "result" in j or "error" in j:
                    return j
        try:
            return json.loads(data)
        except Exception:
            return {"raw": data[:2000]}

    def call(self, tool: str, arguments: dict):
        """Call an OpusClip MCP tool; return parsed JSON payload or raise OpusError."""
        out = self._raw("tools/call", {"name": tool, "arguments": arguments})
        if not out:
            raise OpusError(f"{tool}: empty response")
        if "error" in out:
            raise OpusError(f"{tool}: {json.dumps(out['error'])[:300]}")
        res = out.get("result", {})
        if res.get("isError"):
            txt = "".join(c.get("text", "") for c in res.get("content", []))
            raise OpusError(f"{tool}: {txt[:400]}")
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        blob = "\n".join(texts).strip()
        if not blob:
            return {}
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return {"text": blob[:4000]}


# ---------------------------------------------------------------- helpers


def _find_items(payload):
    """OpusClip responses nest lists inconsistently; find the first list of dicts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
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


# ---------------------------------------------------------------- operations


def credits() -> dict:
    c = OpusClient()
    usage = c.call("opusclip_get_usage", {})
    out = {"raw": usage}
    # NOTE: get_usage can lie; a submit rejection is the source of truth.
    out["warning"] = "If submit rejects with 'insufficient credits', TRUST THE REJECTION over this report."
    return out


def list_accounts() -> dict:
    c = OpusClient()
    payload = c.call("opusclip_list_social_accounts", {})
    items = _find_items(payload) or []
    found = {}
    for it in items:
        for p in PLATFORMS:
            if _match_platform(it, p) and p not in found:
                found[p] = it
    return {"connected": sorted(found.keys()), "accounts": items, "raw_type": type(payload).__name__}


def upload(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"file not found: {p}"}
    size_mb = max(1, round(p.stat().st_size / (1024 * 1024)))
    ext = p.suffix.lstrip(".").lower() or "mp4"
    c = OpusClient()
    link = c.call("opusclip_create_upload_link", {"extension": ext, "sizeMb": size_mb})
    upload_url = link.get("upload_url") or link.get("uploadUrl") or (link.get("data") or {}).get("upload_url")
    upload_id = link.get("upload_id") or link.get("uploadId") or (link.get("data") or {}).get("upload_id")
    if not upload_url or not upload_id:
        return {"error": "no upload_url/upload_id in response", "raw": link}
    # resumable: POST start -> Location -> PUT binary. Never one-shot PUT (400s).
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
    return {"upload_id": upload_id, "size_mb": size_mb, "hint": "upload_id stays valid for days; pass it to publish_submit as videoUrl"}


def submit(upload_id: str, title: str, poll_s: int = 240) -> dict:
    c = OpusClient()
    proj = c.call(
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
    project_id = proj.get("projectId") or proj.get("project_id") or (proj.get("data") or {}).get("projectId")
    if not project_id:
        msg = json.dumps(proj)[:400]
        if "credit" in msg.lower():
            # trust the rejection over get_usage
            return {"error": "SUBMIT_REJECTED_CREDITS", "detail": msg, "action": "top up OpusClip; the upload_id stays valid, retry submit afterwards"}
        return {"error": "no projectId in submit response", "raw": proj}
    deadline = time.time() + poll_s
    clip = None
    while time.time() < deadline:
        time.sleep(20)
        try:
            payload = c.call("opusclip_list_clips", {"projectId": project_id})
        except OpusError:
            continue
        items = _find_items(payload) or []
        for it in items:
            # poll on the clip_id KEY — grepping "id" never matches
            if it.get("clip_id") or it.get("clipId"):
                clip = it
                break
        if clip:
            break
    if not clip:
        return {
            "projectId": project_id,
            "status": "STILL_PROCESSING",
            "hint": "processing can stall platform-side; re-check with publish_status or list_clips later. The upload is NOT lost.",
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


def schedule(project_id: str, clip_id: str, publish_at: str, platforms=None, copy=None, title="", llm=None) -> dict:
    c = OpusClient()
    accounts = list_accounts()
    connected = accounts["connected"]
    want = [p for p in (platforms or connected) if p in connected]
    if not want:
        return {"error": "no matching connected platforms", "connected": connected, "requested": platforms}
    target = _parse_utc(publish_at)
    # collision check ±2h, shift by 30 min until clear (max ±3h)
    window_start = (target - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = (target + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        existing = _find_items(c.call("opusclip_list_scheduled_posts", {"startAt": window_start, "endAt": window_end})) or []
    except OpusError:
        existing = []
    shifts = [0, 30, -30, 60, -60, 90, -90, 120, -120, 150, -150, 180, -180]
    chosen = None
    for sh in shifts:
        cand = target + timedelta(minutes=sh)
        clash = any(
            _match_platform(e, p) and e.get("publish_at") and abs((_parse_utc(e["publish_at"]) - cand).total_seconds()) < 2700
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
            r = c.call("opusclip_schedule_publish", args)
            results[p] = {"scheduled": True, "has_conflict": r.get("has_conflict", r.get("hasConflict")), "raw": r}
        except OpusError as e:
            results[p] = {"scheduled": False, "error": str(e)[:250]}
    # read-back verify
    time.sleep(3)
    try:
        verify = _find_items(c.call("opusclip_list_scheduled_posts", {"projectId": project_id})) or []
    except OpusError:
        verify = []
    ok = sum(1 for v in results.values() if v.get("scheduled"))
    return {
        "publish_at_utc": chosen.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scheduled": ok,
        "failed": len(results) - ok,
        "per_platform": {p: {k: v for k, v in r.items() if k != "raw"} for p, r in results.items()},
        "verified_entries": len(verify),
        "copy_used": {p: {"title": c2["title"][:80]} for p, c2 in final_copy.items()},
    }


def status(project_id: str = "", start_at: str = "", end_at: str = "") -> dict:
    c = OpusClient()
    args = {}
    if project_id:
        args["projectId"] = project_id
    if start_at and end_at:
        args["startAt"], args["endAt"] = start_at, end_at
    items = _find_items(c.call("opusclip_list_scheduled_posts", args)) or []
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


def unschedule(schedule_id: str) -> dict:
    c = OpusClient()
    try:
        r = c.call("opusclip_unschedule_publish", {"scheduleId": schedule_id})
        return {"unscheduled": True, "schedule_id": schedule_id, "raw": r}
    except OpusError as e:
        msg = str(e)
        if "already completed" in msg.lower():
            return {"error": "ALREADY_PUBLISHED", "hint": "check `date` vs publish_at; remediation is per-platform (YT descriptions editable via Data API; X/TikTok/IG/FB posts are not editable via API)"}
        return {"error": msg[:300]}
