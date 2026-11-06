---
name: tightbeam
description: "Tightbeam setup and workflow: YouTube sign-in, OpusClip MCP, sweeps, publishing."
version: 0.4.0
metadata:
  hermes:
    tags: [youtube, publishing, opusclip, metadata, sweep]
---

# Tightbeam

Beam your content out: publish vertical shorts to every connected platform
through OpusClip, and keep your whole YouTube catalog optimized — new uploads
as they land, back catalog night by night under a quota budget.

## One-time setup

Run `tightbeam_status` first; it probes both pillars and prints the exact
missing step.

### YouTube (optimizer pillar)

1. Google Cloud Console: create a project, enable **YouTube Data API v3**.
2. Create an **OAuth client ID** of type *Desktop app*.
3. Publish the OAuth app to **Production** (Auth Platform → Audience →
   Publish App) or refresh tokens die after 7 days.
4. Add to `~/.hermes/.env`:
   `TIGHTBEAM_GOOGLE_CLIENT_ID` and `TIGHTBEAM_GOOGLE_CLIENT_SECRET`.
5. `hermes auth add tightbeam:google` — browser sign-in; pick the right
   Google account (incognito if several). Hermes stores and refreshes the
   token; the plugin never sees it.
6. Re-auth / refresh / logout: `hermes auth status|refresh|logout tightbeam:google`.

### OpusClip (publishing pillar)

1. `hermes mcp add opusclip --url https://mcp.opus.pro/mcp --auth oauth`
2. `hermes mcp login opusclip` (interactive browser OAuth; crons cannot).
3. Connect your social accounts at opus.pro.
4. Tightbeam dispatches through this connection (`mcp__opusclip__*` tools);
   it never touches the token itself.

## Daily workflows

- **New upload**: `yt_watch` → `yt_optimize` per video (safe mode = tags +
  defaults; add `fields:["title"]` for LLM-scored title swaps) → verify.
- **Back catalog**: `yt_audit` once, then the `catalog-sweep` blueprint
  nightly with a budget (each `videos.update` costs 50 units of 10,000/day;
  default cap 60 updates/day).
- **Short drop**: `publish_upload` → `publish_submit` (passthrough, captions
  OFF) → vision-check the preview → `publish_schedule` (collision-checked,
  cap-enforced, read-back verified).
- **Morning audit**: `publish_status` over yesterday's window — surfaces
  silent per-platform failures (expired/revoked OAuth) with the fix.

## Hard-won rules baked into the tools

- Facebook titles ≤255 chars, X ≤280, no `>` in YouTube descriptions.
- `enableCaption:false` on EVERY passthrough submit or OpusClip burns a
  second caption layer over yours.
- Trust the submit rejection over `get_usage` — the credit report can lie.
- Two OpusClip pools gate separately: monthly credits vs processing minutes.
- 412 on `videos.update` is transient: retried with 8/16/24s backoff.
- Never schedule without a ±2h collision check per account.
- The upload_id from `publish_upload` stays valid for days; after a credit
  block or auth fix, retry `publish_submit` with it — never re-upload.
- TEST_AND_COMPARE (Studio A/B) locks titles via API: the optimizer falls
  back to tags-only automatically.
