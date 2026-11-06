# Tightbeam ☤🚀

> "Flip and burn." — beam your shorts to every platform and keep your whole
> YouTube catalog optimized, on autopilot.

Tightbeam is a [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin
built on production pipelines that run real channels (300K+ subs): the nightly
catalog sweep that quietly improves every old video, the post-publish optimizer
that catches new uploads within hours, and the multi-platform shorts publisher
that ships one vertical to YouTube, TikTok, Instagram, Facebook, X and LinkedIn
with collision checks and platform caps enforced in code.

## What it does

**11 tools, two pillars:**

| Pillar | Tools |
| --- | --- |
| YouTube optimizer | `yt_audit`, `yt_watch`, `yt_optimize`, `yt_sweep` |
| Multi-platform publishing (OpusClip) | `publish_upload`, `publish_submit`, `publish_schedule`, `publish_status`, `publish_unschedule` |
| Ops | `tightbeam_status`, `tightbeam_credits` |

**Two one-click automation blueprints** (appear in `/blueprint`, the dashboard,
and the Desktop Cron page):

- **Post-publish optimizer** — daily: find uploads since the last run, optimize
  metadata, verify, report.
- **Catalog sweep** — nightly: work through your back catalog under a quota
  budget, fully resumable. A 750-video channel gets fully swept in weeks
  without a single manual action.

Title generation and per-platform copy use the host LLM (`ctx.llm`) — no
third-party SEO subscription required.

## Setup

```bash
hermes plugins install skeptical-agency/tightbeam --enable
tightbeam_status   # guided health check; prints the exact missing step
```

**YouTube pillar** (optimizer): bring your own Google Cloud OAuth client
(YouTube Data API v3 enabled, Desktop client, app published to Production so
refresh tokens don't expire weekly):

```bash
# .env
TIGHTBEAM_GOOGLE_CLIENT_ID=***
TIGHTBEAM_GOOGLE_CLIENT_SECRET=***

hermes auth add tightbeam:google   # one-time browser sign-in
```

Hermes owns the grant: it stores and refreshes the token, and the plugin never
sees it (all calls go through the host's credentialed request helper).

**OpusClip pillar** (publishing): connect the official OpusClip MCP server —
the same connection Hermes manages for any other MCP tool:

```bash
hermes mcp add opusclip --url https://mcp.opus.pro/mcp --auth oauth
hermes mcp login opusclip          # one-time browser sign-in
```

Tightbeam dispatches through that connection; it never reads or refreshes
OpusClip credentials itself. Connect your social accounts at opus.pro.

## Reliability rules baked in (learned the hard way)

- Facebook title ≤255, X ≤280, no `>` in YouTube descriptions — asserted
  before scheduling, not after a silent publish failure.
- Passthrough submits force `enableCaption:false` (no double-caption layers).
- Transient HTTP 412 on `videos.update` retried with backoff.
- ±2h collision check per account before every schedule, 30-min shifts.
- Quota budgeting against the real 10,000-unit/day YouTube cap; sweep state
  survives restarts and profiles.
- Read-back verification after every write; honest failure reporting per
  platform (silent OAuth-expiry/revocation failures surfaced with the exact
  reconnect action).
- Credit rejections trusted over usage reports (the report can lie).

## Requirements

- Hermes with plugin support; no pip dependencies (pure stdlib + host APIs).
- A Google Cloud project with YouTube Data API v3 + Desktop OAuth client
  (optimizer pillar; free-tier quota is fine).
- An [OpusClip](https://opus.pro) account with social accounts connected
  (publishing pillar).

## Disclosure

- Video metadata (titles, descriptions, tags, video ids) is sent to the Google
  YouTube Data API through your own OAuth client; video files (bytes uploaded
  to the signed URL OpusClip returns) and post copy are sent to OpusClip when
  publishing.
- Title/copy generation uses your Hermes host LLM under its own credentials.
- The plugin stores NO credentials. The YouTube grant lives in Hermes's auth
  store (`hermes auth status tightbeam:google`), the OpusClip token in the
  Hermes-managed MCP connection. Plugin state (audit, sweep progress,
  watermark) lives in `<HERMES_HOME>/plugin-data/tightbeam/`.
- Publishing actions are irreversible once the platform window passes; the
  plugin verifies state but cannot un-publish.

## Status

v0.4.0 — both pillars live-tested against a production 300K-sub channel and a
production OpusClip account (real audit, watch, sweep update, credits,
schedule-status reads; the failure detector caught a live TikTok revocation).

Built by [Skeptical](https://skeptical.agency) for the Hermes Plugin Hackathon.
