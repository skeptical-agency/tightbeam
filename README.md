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

**13 tools, two pillars:**

| Pillar | Tools |
| --- | --- |
| YouTube optimizer | `yt_auth_url`, `yt_auth_code`, `yt_audit`, `yt_watch`, `yt_optimize`, `yt_sweep` |
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
third-party SEO subscription required. VidIQ is used automatically when the
user has it connected.

## Reliability rules baked in (learned the hard way)

- Facebook title ≤255, X ≤280, no `>` in YouTube descriptions — asserted
  before scheduling, not after a silent publish failure.
- Passthrough submits force `enableCaption:false` (no double-caption layers).
- Transient HTTP 412 on `videos.update` retried with backoff.
- ±2h collision check per account before every schedule.
- Quota budgeting against the real 10,000-unit/day YouTube cap; sweep state
  survives restarts.
- Read-back verification after every write; honest failure reporting per
  platform (silent OAuth-expiry failures surfaced with the exact reconnect
  action).

## Install

```bash
hermes plugins install skeptical-agency/tightbeam --enable
```

Then run `tightbeam_status` for a guided health check. Setup details (Google
OAuth client, OpusClip login) live in the bundled `tightbeam` skill.

## Requirements

- A Google Cloud project with YouTube Data API v3 + a Desktop OAuth client
  (free tier quota is fine).
- An [OpusClip](https://opus.pro) account with social accounts connected
  (publishing pillar only).

## Disclosure

- Video metadata (titles, descriptions, tags, video ids) is sent to the Google
  YouTube Data API; video files and copy are sent to OpusClip when publishing.
- Title/copy generation uses your Hermes host LLM under its credentials.
- OAuth tokens are stored locally in the plugin data dir
  (`<HERMES_HOME>/plugin-data/tightbeam/`), never transmitted elsewhere.
- Publishing actions are irreversible once the platform window passes; the
  plugin verifies state but cannot un-publish.

## Status

v0.1.0 — scaffold: manifest, tool surface, blueprints, bundled skill.
Handlers land through v0.2–v0.4 (optimizer → publisher → polish).

Built by [Skeptical](https://skeptical.agency) for the Hermes Plugin Hackathon.
