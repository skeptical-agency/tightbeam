"""Tightbeam YouTube engine — Data API v3 over Hermes-owned OAuth.

RULE 11 COMPLIANT: this module never sees a token. The OAuth grant is declared
under `oauth:` in plugin.yaml, signed in with `hermes auth add tightbeam:google`,
stored and refreshed by Hermes, and attached by credentialed_provider_request().
All calls are plain REST to www.googleapis.com (a declared host).

State lives in <data_dir>/state.json:
  {"channel": {...}, "audit": {"ts", "videos": [...]},
   "sweep": {"done": [...], "log": [...]}, "watch": {"watermark": iso}}
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

API = "https://www.googleapis.com/youtube/v3"
PROVIDER = "google"  # the oauth: entry id in plugin.yaml

STOP = set(
    """a an the and or of to in on for with is are was were be been this that these those
it its you your we our they them i my me he she his her at by from as but not no yes do does did
new ever most best top big huge all every full official just now here there what when how why who
which can could should would will shall may might must lets vs versus ft feat featuring ep episode
part pt vol chapter update news recap roundup review unboxing hands first look gameplay trailer
teaser reveal announced announcement reveals announcements leak leaks rumor rumors says said
confirms confirmed coming soon today tomorrow yesterday week month year day live stream shorts
short video videos everything nothing something anything more less than over under""".split()
)

TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "current_score": {"type": "integer", "description": "0-100 score of the current title"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "score": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["title", "score"],
            },
        },
    },
    "required": ["current_score", "candidates"],
}

AUTH_HINT = (
    "Not signed in. Setup: set TIGHTBEAM_GOOGLE_CLIENT_ID / TIGHTBEAM_GOOGLE_CLIENT_SECRET "
    "in .env (your own Google Cloud Desktop OAuth client, YouTube Data API v3 enabled, app "
    "published to Production), then run: hermes auth add tightbeam:google"
)


class QuotaError(RuntimeError):
    pass


class NotSignedIn(RuntimeError):
    pass


# ---------------------------------------------------------------- plumbing


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # documented public API

        return Path(plugin_data_dir("tightbeam"))
    except Exception:
        home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        p = Path(home) / "plugin-data" / "tightbeam"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _cfg(key, default, ctx=None):
    if ctx is not None:
        try:
            return ctx.get_config(key, default=default)
        except Exception:
            pass
    return default


def load_state() -> dict:
    p = _data_dir() / "state.json"
    if p.exists():
        try:
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    return {"channel": {}, "audit": {}, "sweep": {"done": [], "log": []}, "watch": {}}


def save_state(st: dict) -> None:
    p = _data_dir() / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(p)


def _api(method: str, path: str, params: dict | None = None, body: dict | None = None, retries: int = 4) -> dict:
    """One YouTube Data API call through Hermes-owned credentials (rule 11)."""
    from hermes_cli.plugin_provider_requests import (  # documented public helper
        ProviderNotSignedIn,
        credentialed_provider_request,
    )

    url = f"{API}/{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
    last = None
    for attempt in range(retries):
        try:
            resp = credentialed_provider_request(PROVIDER, method, url, json=body, timeout=60)
        except ProviderNotSignedIn as exc:
            raise NotSignedIn(f"{AUTH_HINT} ({exc})")
        if resp.status < 300:
            try:
                data = resp.json()
            except Exception:
                return {"raw": (resp.text or "")[:500]}
            return data if isinstance(data, dict) else {"raw": data}
        text = (resp.text or "")[:400]
        if "quotaExceeded" in text or "dailyLimitExceeded" in text:
            raise QuotaError(text)
        if resp.status == 401:
            raise NotSignedIn(f"401 from YouTube API — try: hermes auth refresh tightbeam:google. {text[:150]}")
        last = f"HTTP {resp.status}: {text}"
        if resp.status in (412, 400, 500, 503):
            time.sleep(8 * (attempt + 1))  # 412 precondition is transient: 8/16/24s backoff
            continue
        raise RuntimeError(last)
    raise RuntimeError(f"failed after {retries} attempts: {last}")


def _parse_duration(iso: str) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def derive_tags(title: str, desc: str, kind: str, channel_tag: str = "") -> list:
    """Rule-based tags from title + description. No external spend."""
    tags = [re.sub(r"#\w+", "", title).lower().strip()]
    cleaned = re.sub(r"[^a-z0-9 ]", " ", title.lower()).strip()
    words = [w for w in cleaned.split() if w not in STOP and len(w) > 2]
    for n in (3, 2):
        for i in range(len(words) - n + 1):
            gram = " ".join(words[i : i + n])
            if gram not in tags:
                tags.append(gram)
    dw = [w for w in re.sub(r"[^a-z0-9 ]", " ", desc[:300].lower()).split() if w not in STOP and len(w) > 3]
    for w in dict.fromkeys(dw):
        if w not in tags:
            tags.append(w)
        if len(tags) > 22:
            break
    if kind == "short":
        tags.append("shorts")
    if channel_tag:
        tags.append(channel_tag.lower())
    seen, out, total = set(), [], 0
    for t in tags:
        t = t.strip()
        if not t or t in seen or len(t) > 60:
            continue
        if total + len(t) > 480:  # YouTube 500-char TOTAL tag limit
            continue
        seen.add(t)
        out.append(t)
        total += len(t)
    return out


# ---------------------------------------------------------------- auth status


def check() -> dict:
    data = _api("GET", "channels", {"part": "snippet", "mine": "true"})
    items = data.get("items", [])
    if not items:
        return {"error": "NO_CHANNEL"}
    ch = items[0]
    return {"channel_id": ch["id"], "title": ch["snippet"]["title"]}


# ---------------------------------------------------------------- audit / watch


def _uploads_playlist() -> tuple:
    data = _api("GET", "channels", {"part": "snippet,contentDetails", "mine": "true"})
    items = data.get("items", [])
    if not items:
        raise RuntimeError("NO_CHANNEL on this grant")
    ch = items[0]
    return ch["id"], ch["snippet"]["title"], ch["contentDetails"]["relatedPlaylists"]["uploads"]


def _fetch_videos(ids: list) -> list:
    out = []
    for i in range(0, len(ids), 50):
        data = _api("GET", "videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i : i + 50])})
        out.extend(data.get("items", []))
    return out


def _row(v: dict) -> dict:
    dur = _parse_duration(v.get("contentDetails", {}).get("duration", ""))
    kind = "short" if dur <= 180 else ("live" if dur > 3600 else "long")
    sn = v.get("snippet", {})
    st = v.get("statistics", {})
    return {
        "id": v["id"],
        "title": sn.get("title", ""),
        "published": sn.get("publishedAt", ""),
        "views": int(st.get("viewCount", 0)),
        "kind": kind,
        "has_tags": bool(sn.get("tags")),
        "has_desc": len(sn.get("description", "") or "") > 40,
    }


def audit(max_videos: int = 0) -> dict:
    st = load_state()
    ch_id, ch_title, uploads = _uploads_playlist()
    ids, token = [], None
    while True:
        data = _api("GET", "playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 50, "pageToken": token})
        for it in data.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)
        if max_videos and len(ids) >= max_videos:
            ids = ids[:max_videos]
            break
        token = data.get("nextPageToken")
        if not token:
            break
    rows = [_row(v) for v in _fetch_videos(ids)]
    summary = {
        "total": len(rows),
        "shorts": sum(1 for r in rows if r["kind"] == "short"),
        "longs": sum(1 for r in rows if r["kind"] == "long"),
        "lives": sum(1 for r in rows if r["kind"] == "live"),
        "missing_tags": sum(1 for r in rows if not r["has_tags"]),
        "missing_desc": sum(1 for r in rows if not r["has_desc"]),
    }
    st["channel"] = {"id": ch_id, "title": ch_title}
    st["audit"] = {"ts": datetime.now(timezone.utc).isoformat(), "videos": rows}
    save_state(st)
    return {"channel": st["channel"], "audited_at": st["audit"]["ts"], "summary": summary}


def watch(lookback_hours: int = 0) -> dict:
    st = load_state()
    _, _, uploads = _uploads_playlist()
    now = datetime.now(timezone.utc)
    wm_iso = st.get("watch", {}).get("watermark")
    if lookback_hours:
        cutoff = now - timedelta(hours=lookback_hours)
    elif wm_iso:
        cutoff = datetime.fromisoformat(wm_iso)
    else:
        cutoff = now - timedelta(hours=48)
    new, token, pages = [], None, 0
    while pages < 4:  # uploads playlist is newest-first; 200 videos is plenty
        data = _api("GET", "playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 50, "pageToken": token})
        pages += 1
        stop = False
        for it in data.get("items", []):
            pub = it.get("contentDetails", {}).get("videoPublishedAt", "")
            vid = it.get("contentDetails", {}).get("videoId")
            if not pub or not vid:
                continue
            if datetime.fromisoformat(pub.replace("Z", "+00:00")) <= cutoff:
                stop = True
                break
            new.append(vid)
        token = data.get("nextPageToken")
        if stop or not token:
            break
    fresh = [_row(v) for v in _fetch_videos(new)] if new else []
    if fresh:
        st.setdefault("watch", {})["watermark"] = max(r["published"] for r in fresh)
    elif not wm_iso:
        st.setdefault("watch", {})["watermark"] = now.isoformat()
    save_state(st)
    return {"cutoff": cutoff.isoformat(), "new_videos": fresh, "count": len(fresh)}


# ---------------------------------------------------------------- optimize


def _llm_title(llm, title: str, desc: str, kind: str):
    """Ask the host LLM for scored title candidates. Returns dict or None."""
    if llm is None:
        return None
    try:
        res = llm.complete_structured(
            instructions=(
                "You are a YouTube growth strategist. Given a video's current title, description "
                f"and format ({kind}), produce 5 alternative titles (max 95 chars each, no emoji, "
                "no ALL-CAPS spam, honest to the content, main keyword early) and score each 0-100 "
                "on click-through potential + search clarity. Also score the CURRENT title the same way."
            ),
            input=[{"type": "text", "text": json.dumps({"current_title": title, "description": desc[:1200]})}],
            json_schema=TITLE_SCHEMA,
            purpose="tightbeam.title-gen",
            temperature=0.5,
            max_tokens=700,
        )
        parsed = getattr(res, "parsed", None)
        if not parsed or not parsed.get("candidates"):
            return None
        best = max(parsed["candidates"], key=lambda c: c.get("score", 0))
        if best.get("score", 0) <= parsed.get("current_score", 100):
            return {"kept": True, "reason": "current title outscores candidates", "current_score": parsed.get("current_score")}
        if best.get("score", 0) < 70:
            return {"kept": True, "reason": "no candidate reached 70", "best": best}
        return {"kept": False, "new_title": best["title"][:95], "score": best["score"], "current_score": parsed.get("current_score")}
    except Exception:
        return None


def optimize(video_id: str, fields=None, dry_run: bool = False, llm=None, ctx=None) -> dict:
    fields = set(fields or ["tags", "defaults"])
    st = load_state()
    channel_tag = _cfg("tag_channel", "", ctx)
    default_category = str(_cfg("default_category", "", ctx) or "")
    title_source = _cfg("title_source", "llm", ctx)
    data = _api("GET", "videos", {"part": "snippet,status", "id": video_id})
    items = data.get("items", [])
    if not items:
        return {"error": "video not found", "video_id": video_id}
    v = items[0]
    snippet, status = v["snippet"], v["status"]
    kind_row = next((r for r in st.get("audit", {}).get("videos", []) if r["id"] == video_id), None)
    kind = kind_row["kind"] if kind_row else ("short" if "#shorts" in snippet.get("title", "").lower() else "long")
    changes = {}

    if "tags" in fields and not snippet.get("tags"):
        changes["tags"] = derive_tags(snippet.get("title", ""), snippet.get("description", ""), kind, channel_tag)
    if "title" in fields and title_source == "llm":
        t = _llm_title(llm, snippet.get("title", ""), snippet.get("description", ""), kind)
        if t and not t.get("kept"):
            changes["title"] = t["new_title"]
            changes["_title_meta"] = t
        elif t:
            changes["_title_meta"] = t
    if "defaults" in fields and default_category and not snippet.get("categoryId"):
        changes["categoryId"] = default_category

    write_keys = {k for k in changes if not k.startswith("_")}
    if not write_keys:
        return {"video_id": video_id, "status": "no_changes", "meta": changes.get("_title_meta")}
    if dry_run:
        return {"video_id": video_id, "status": "dry_run", "planned": {k: changes[k] for k in write_keys}}

    new_snippet = dict(snippet)
    for k in ("tags", "title", "categoryId"):
        if k in changes:
            new_snippet[k] = changes[k]
    body = {
        "id": video_id,
        "snippet": new_snippet,
        "status": {k: status[k] for k in ("privacyStatus", "embeddable", "license", "publicStatsViewable", "madeForKids") if k in status},
    }
    try:
        _api("PUT", "videos", {"part": "snippet,status"}, body)
    except QuotaError as e:
        return {"error": "QUOTA_EXCEEDED", "detail": str(e)[:200], "video_id": video_id}
    except RuntimeError as e:
        msg = str(e)
        if "TEST_AND_COMPARE" in msg and "title" in changes:
            new_snippet["title"] = snippet.get("title")  # A/B test locks titles; tags-only fallback
            body["snippet"] = new_snippet
            changes.pop("title", None)
            try:
                _api("PUT", "videos", {"part": "snippet,status"}, body)
            except Exception as e2:
                return {"error": str(e2)[:300], "video_id": video_id}
            changes["title_blocked"] = "test-and-compare active"
        else:
            return {"error": msg[:300], "video_id": video_id}
    time.sleep(2)
    back = _api("GET", "videos", {"part": "snippet", "id": video_id})["items"][0]["snippet"]
    verified = {"title": back.get("title"), "n_tags": len(back.get("tags", []))}
    st = load_state()
    st["sweep"]["log"].append({"id": video_id, "action": ",".join(sorted(write_keys)), "ts": time.strftime("%Y-%m-%d %H:%M")})
    if video_id not in st["sweep"]["done"]:
        st["sweep"]["done"].append(video_id)
    save_state(st)
    return {
        "video_id": video_id,
        "status": "updated",
        "changes": {k: (f"{len(val)} tags" if k == "tags" else val) for k, val in changes.items()},
        "verified": verified,
    }


# ---------------------------------------------------------------- sweep


def sweep(budget_updates: int = 10, mode: str = "both", dry_run: bool = False, llm=None, ctx=None) -> dict:
    st = load_state()
    audit_rows = st.get("audit", {}).get("videos", [])
    if not audit_rows:
        return {"error": "NO_AUDIT", "hint": "run yt_audit first"}
    daily_cap = int(_cfg("daily_update_cap", 60, ctx) or 60)
    today = time.strftime("%Y-%m-%d")
    used_today = sum(1 for e in st["sweep"]["log"] if e.get("ts", "").startswith(today))
    remaining = max(0, daily_cap - used_today)
    if remaining == 0:
        return {"status": "DAILY_BUDGET_EXHAUSTED", "used_today": used_today, "daily_cap": daily_cap}
    budget = min(int(budget_updates or 10), remaining)
    done = set(st["sweep"]["done"])
    if mode in ("tags", "both"):
        targets = [r for r in audit_rows if r["id"] not in done and not r["has_tags"]]
    else:
        targets = [r for r in audit_rows if r["id"] not in done]
    targets.sort(key=lambda r: r["views"], reverse=True)
    targets = targets[:budget]
    results = {"ok": 0, "no_changes": 0, "fail": 0, "quota_stop": False, "updates": [], "used_today": used_today, "budget": budget}
    if dry_run:
        return {"status": "dry_run", "would_process": [{"id": t["id"], "title": t["title"][:60], "views": t["views"]} for t in targets]}
    for t in targets:
        if mode == "titles":
            fields = ["title", "defaults"]
        elif mode == "tags":
            fields = ["tags", "defaults"]
        else:
            fields = ["tags", "title", "defaults"]
        try:
            r = optimize(t["id"], fields=fields, dry_run=False, llm=llm, ctx=ctx)
        except QuotaError:
            results["quota_stop"] = True
            break
        if r.get("error") == "QUOTA_EXCEEDED":
            results["quota_stop"] = True
            break
        if r.get("error"):
            results["fail"] += 1
            results["updates"].append({"id": t["id"], "error": r["error"][:150]})
        elif r.get("status") == "no_changes":
            results["no_changes"] += 1
            st = load_state()
            if t["id"] not in st["sweep"]["done"]:
                st["sweep"]["done"].append(t["id"])
                save_state(st)
        else:
            results["ok"] += 1
            acted = ",".join(k for k in r.get("changes", {}) if not k.startswith("_"))
            results["updates"].append({"id": t["id"], "action": acted})
        time.sleep(3)  # write-consistency spacing
    st = load_state()
    results["total_done"] = len(st["sweep"]["done"])
    results["backlog_no_tags"] = sum(
        1 for r in st.get("audit", {}).get("videos", []) if not r["has_tags"] and r["id"] not in set(st["sweep"]["done"])
    )
    return results
