---
name: tightbeam
description: "Tightbeam setup and workflow: YouTube OAuth, OpusClip auth, sweeps, publishing."
version: 0.1.0
metadata:
  hermes:
    tags: [youtube, publishing, opusclip, metadata, sweep]
---

# Tightbeam

Beam your content out: publish vertical shorts to every connected platform
through OpusClip, and keep your whole YouTube catalog optimized — new uploads
as they land, back catalog night by night under a quota budget.

## One-time setup

### YouTube (optimizer pillar)

1. Create a Google Cloud project, enable the **YouTube Data API v3**.
2. Create an **OAuth client ID** of type *Desktop app*; download the JSON.
3. Publish the OAuth app to **Production** (Auth Platform → Audience → Publish)
   or refresh tokens die after 7 days.
4. Put the JSON at the Tightbeam data dir as `client_secret.json`
   (`tightbeam_status` prints the exact path).
5. Run `yt_auth_url`, open the returned URL (incognito if multiple Google
   accounts), approve, paste the full redirect URL into `yt_auth_code`.
   The dead `localhost` landing page after consent is expected.

### OpusClip (publishing pillar)

1. `hermes mcp login opusclip` (interactive browser OAuth; crons cannot do this).
2. Connect your social accounts at opus.pro.
3. `tightbeam_status` confirms the token is visible.

## Daily workflows

- **New upload**: `yt_watch` → `yt_optimize` per video → verify read-back.
- **Back catalog**: `yt_audit` once, then the `catalog-sweep` blueprint nightly
  with a budget (each `videos.update` costs 50 quota units of the 10,000/day).
- **Short drop**: `publish_upload` → `publish_submit` (passthrough, captions
  OFF) → vision-check the preview → `publish_schedule` (collision-checked,
  cap-enforced, read-back verified).

## Hard-won rules baked into the tools

- Facebook titles ≤255 chars, X ≤280, no `>` in YouTube descriptions.
- `enableCaption:false` on EVERY passthrough submit or OpusClip burns a
  second caption layer over yours.
- Trust the submit rejection over `get_usage` — the credit report can lie.
- Two OpusClip pools gate separately: monthly credits vs processing minutes.
- 412 on `videos.update` is transient: retry with 8/16/24s backoff.
- Never schedule without a ±2h collision check per account.
