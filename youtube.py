"""Tightbeam YouTube engine — Data API v3 operations.

Ported and generalized from production pipelines (300K-sub channel):
PKCE OAuth, catalog audit, new-upload watch, single-video optimize, and a
budgeted resumable sweep. All functions return plain dicts; handlers in
tools.py serialize them.

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

SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPES = [SCOPE]
REDIRECT_URI = "http://localhost"

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


class QuotaError(RuntimeError):
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


def _client_secret_path() -> Path:
    return _data_dir() / "client_secret.json"


def _token_path() -> Path:
    return _data_dir() / "youtube_token.json"


def _state_path() -> Path:
    return _data_dir() / "state.json"


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"channel": {}, "audit": {}, "sweep": {"done": [], "log": []}, "watch": {}}


def save_state(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(p)


def _creds():
    from google.oauth2.credentials import Credentials

    tp = _token_path()
    if not tp.exists():
        raise RuntimeError(
            f"NOT_AUTHENTICATED: no token at {tp}. Run yt_auth_url, approve in a browser, "
            "then yt_auth_code with the redirect URL."
        )
    creds = Credentials.from_authorized_user_file(str(tp), [SCOPE])
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        tp.write_text(creds.to_json())
    return creds


def _yt():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def _update_video(yt, video_id: str, body: dict, part: str, retries: int = 4):
    """videos.update with 412 backoff (transient) and quota detection (fatal)."""
    last = None
    for attempt in range(retries):
        try:
            return yt.videos().update(part=part, body=body).execute()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "quotaExceeded" in msg or "daily quota" in msg.lower():
                raise QuotaError(msg)
            last = e
            status = getattr(e, "resp", {}).get("status", "")
            if status == "412" or "Precondition" in msg:
                time.sleep(8 * (attempt + 1))  # 8/16/24s backoff
                continue
            if status in ("400", "500", "503"):
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"update failed after {retries} attempts: {last}")


def _parse_duration(iso: str) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def derive_tags(title: str, desc: str, kind: str, channel_tag: str = "") -> list:
    """Rule-based tags from title + description. No external spend."""
    text = f"{title} {desc[:400]}".lower()
    tags = []
    tags.append(re.sub(r"#\w+", "", title).lower().strip())
    cleaned = re.sub(r"[^a-z0-9 ]", " ", title.lower()).strip()
    words = [w for w in cleaned.split() if w not in STOP and len(w) > 2]
    for n in (3, 2):
        for i in range(len(words) - n + 1):
            gram = " ".join(words[i : i + n])
            if gram not in tags:
                tags.append(gram)
    # salient single words from the description opening
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
    # dedupe + enforce YouTube's 500-char TOTAL limit
    seen, out, total = set(), [], 0
    for t in tags:
        t = t.strip()
        if not t or t in seen or len(t) > 60:
            continue
        if total + len(t) > 480:
            continue
        seen.add(t)
        out.append(t)
        total += len(t)
    return out


# ---------------------------------------------------------------- auth


def auth_url(client_secret_file: str = "") -> dict:
    from google_auth_oauthlib.flow import InstalledAppFlow

    csp = _client_secret_path()
    if client_secret_file:
        src = Path(client_secret_file).expanduser()
        if not src.exists():
            return {"error": f"client_secret_file not found: {src}"}
        csp.parent.mkdir(parents=True, exist_ok=True)
        csp.write_text(src.read_text())
    if not csp.exists():
        return {
            "error": "NO_CLIENT_SECRET",
            "expected_path": str(csp),
            "hint": "Google Cloud Console: enable YouTube Data API v3, create a Desktop OAuth client, "
            "publish the app to Production, download the JSON, then call yt_auth_url with "
            "client_secret_file=/path/to/client_secret.json",
        }
    client_config = json.loads(csp.read_text())
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    url, _ = flow.authorization_url(prompt="consent", access_type="offline", include_granted_scopes="true")
    pending = {"client_config": client_config, "code_verifier": getattr(flow, "code_verifier", None)}
    (_data_dir() / "oauth_pending.json").write_text(json.dumps(pending))
    return {
        "auth_url": url,
        "instructions": "Open this URL (incognito if multiple Google accounts), approve. The dead "
        "localhost landing page afterwards is EXPECTED — paste the full address-bar URL "
        "into yt_auth_code.",
    }


def auth_code(raw: str) -> dict:
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import InstalledAppFlow

    raw = (raw or "").strip()
    code = (parse_qs(urlparse(raw).query).get("code") or [""])[0] if raw.startswith("http") else raw
    if not code:
        return {"error": "no code found in input"}
    pending_path = _data_dir() / "oauth_pending.json"
    saved_verifier = None
    if pending_path.exists():
        pending = json.loads(pending_path.read_text())
        client_config = pending["client_config"]
        saved_verifier = pending.get("code_verifier")
    else:
        csp = _client_secret_path()
        if not csp.exists():
            return {"error": "no pending flow and no client_secret.json; run yt_auth_url first"}
        client_config = json.loads(csp.read_text())
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    if saved_verifier:
        flow.code_verifier = saved_verifier  # attribute name matters; wrong name fails silently
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        return {"error": f"token exchange failed (codes are one-shot; re-run yt_auth_url): {e}"}
    creds = flow.credentials
    payload = json.loads(creds.to_json())
    payload["scopes"] = list(creds.scopes or [SCOPE])
    _token_path().write_text(json.dumps(payload))
    if pending_path.exists():
        pending_path.unlink()
    me = check()
    return {"status": "AUTHENTICATED", "token_path": str(_token_path()), "channel": me}


def check() -> dict:
    yt = _yt()
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        return {"error": "NO_CHANNEL"}
    ch = items[0]
    return {"channel_id": ch["id"], "title": ch["snippet"]["title"]}


# ---------------------------------------------------------------- audit / watch


def _uploads_playlist(yt) -> tuple:
    items = yt.channels().list(part="snippet,contentDetails", mine=True).execute().get("items", [])
    if not items:
        raise RuntimeError("NO_CHANNEL on this token")
    ch = items[0]
    return ch["id"], ch["snippet"]["title"], ch["contentDetails"]["relatedPlaylists"]["uploads"]


def _fetch_videos(yt, ids: list) -> list:
    out = []
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        resp = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(batch)).execute()
        out.extend(resp.get("items", []))
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
    yt = _yt()
    ch_id, ch_title, uploads = _uploads_playlist(yt)
    videos, ids, token = [], [], None
    while True:
        resp = yt.playlistItems().list(
            part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token or ""
        ).execute()
        for it in resp.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)
        if max_videos and len(ids) >= max_videos:
            ids = ids[:max_videos]
            break
        token = resp.get("nextPageToken")
        if not token:
            break
    rows = [_row(v) for v in _fetch_videos(yt, ids)]
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
    yt = _yt()
    _, _, uploads = _uploads_playlist(yt)
    now = datetime.now(timezone.utc)
    wm_iso = st.get("watch", {}).get("watermark")
    if lookback_hours:
        cutoff = now - timedelta(hours=lookback_hours)
    elif wm_iso:
        cutoff = datetime.fromisoformat(wm_iso)
    else:
        cutoff = now - timedelta(hours=48)
    new, token, seen_pages = [], None, 0
    while seen_pages < 4:  # uploads playlist is newest-first; 200 is plenty
        resp = yt.playlistItems().list(
            part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token or ""
        ).execute()
        seen_pages += 1
        stop = False
        for it in resp.get("items", []):
            pub = it.get("contentDetails", {}).get("videoPublishedAt", "")
            vid = it.get("contentDetails", {}).get("videoId")
            if not pub or not vid:
                continue
            pubdt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pubdt <= cutoff:
                stop = True
                break
            new.append({"id": vid, "published": pub})
        token = resp.get("nextPageToken")
        if stop or not token:
            break
    fresh = []
    if new:
        rows = [_row(v) for v in _fetch_videos(yt, [n["id"] for n in new])]
        fresh = rows
    if fresh:
        st.setdefault("watch", {})["watermark"] = max(r["published"] for r in fresh)
    elif not wm_iso:
        st.setdefault("watch", {})["watermark"] = now.isoformat()
    save_state(st)
    return {"cutoff": cutoff.isoformat(), "new_videos": fresh, "count": len(fresh)}


# ---------------------------------------------------------------- optimize


def _llm_title(llm, video_row: dict, desc: str, kind: str):
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
            input=[{"type": "text", "text": json.dumps({"current_title": video_row["title"], "description": desc[:1200]})}],
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
    yt = _yt()
    items = yt.videos().list(part="snippet,status", id=video_id).execute().get("items", [])
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
        t = _llm_title(llm, {"title": snippet.get("title", "")}, snippet.get("description", ""), kind)
        if t and not t.get("kept"):
            changes["title"] = t["new_title"]
            changes["_title_meta"] = t
        elif t:
            changes["_title_meta"] = t
    if "defaults" in fields and default_category and snippet.get("categoryId") in ("", None):
        changes["categoryId"] = default_category

    write_keys = {k for k in changes if not k.startswith("_")}
    if not write_keys:
        return {"video_id": video_id, "status": "no_changes", "meta": changes.get("_title_meta")}
    if dry_run:
        return {"video_id": video_id, "status": "dry_run", "planned": {k: changes[k] for k in write_keys}}

    new_snippet = dict(snippet)
    if "tags" in changes:
        new_snippet["tags"] = changes["tags"]
    if "title" in changes:
        new_snippet["title"] = changes["title"]
    if "categoryId" in changes:
        new_snippet["categoryId"] = changes["categoryId"]
    body = {
        "id": video_id,
        "snippet": new_snippet,
        "status": {k: status[k] for k in ("privacyStatus", "embeddable", "license", "publicStatsViewable", "madeForKids") if k in status},
    }
    try:
        _update_video(yt, video_id, body, "snippet,status")
    except QuotaError as e:
        return {"error": "QUOTA_EXCEEDED", "detail": str(e)[:200], "video_id": video_id}
    except Exception as e:
        msg = str(e)
        if "TEST_AND_COMPARE" in msg and "title" in changes:
            new_snippet["title"] = snippet.get("title")  # A/B test locks titles; fall back to tags-only
            body["snippet"] = new_snippet
            changes.pop("title", None)
            try:
                _update_video(yt, video_id, body, "snippet,status")
            except Exception as e2:
                return {"error": str(e2)[:300], "video_id": video_id}
            changes["title_blocked"] = "test-and-compare active"
        else:
            return {"error": msg[:300], "video_id": video_id}
    time.sleep(2)
    back = yt.videos().list(part="snippet", id=video_id).execute()["items"][0]["snippet"]
    verified = {"title": back.get("title"), "n_tags": len(back.get("tags", []))}
    st["sweep"]["log"].append(
        {"id": video_id, "action": ",".join(sorted(write_keys)), "ts": time.strftime("%Y-%m-%d %H:%M")}
    )
    if video_id not in st["sweep"]["done"]:
        st["sweep"]["done"].append(video_id)
    save_state(st)
    return {"video_id": video_id, "status": "updated", "changes": {k: (v if k != "tags" else f"{len(v)} tags") for k, v in changes.items()}, "verified": verified}


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
        targets.sort(key=lambda r: r["views"], reverse=True)
    else:
        targets = [r for r in audit_rows if r["id"] not in done]
        targets.sort(key=lambda r: r["views"], reverse=True)
    targets = targets[:budget]
    results = {"ok": 0, "no_changes": 0, "fail": 0, "quota_stop": False, "updates": [], "used_today": used_today, "budget": budget}
    if dry_run:
        return {"status": "dry_run", "would_process": [{"id": t["id"], "title": t["title"][:60], "views": t["views"]} for t in targets]}
    for t in targets:
        fields = ["tags", "defaults"] if mode in ("tags", "both") else ["tags", "title", "defaults"]
        if mode == "titles":
            fields = ["title", "defaults"]
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
    results["backlog_no_tags"] = sum(1 for r in st.get("audit", {}).get("videos", []) if not r["has_tags"] and r["id"] not in set(st["sweep"]["done"]))
    return results
