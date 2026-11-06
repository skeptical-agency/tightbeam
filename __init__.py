"""Tightbeam — beam your content out. Registration and wiring."""

from pathlib import Path

from . import schemas, tools


def register(ctx):
    tools.bind_ctx(ctx)

    for name, schema in schemas.ALL.items():
        ctx.register_tool(
            name=name,
            toolset="tightbeam",
            schema=schema,
            handler=tools.HANDLERS[name],
        )

    # One-click automations (appear in /blueprint, dashboard, Desktop Cron page)
    ctx.register_automation_blueprint(
        "post-publish-optimizer",
        title="Tightbeam post-publish optimizer",
        description="Catch new YouTube uploads and optimize their metadata (titles, tags, standing defaults) with verification.",
        category="media",
        schedule_template="{minute} {hour} * * *",
        prompt_template=(
            "Run the Tightbeam post-publish optimizer: call yt_watch to find uploads "
            "since the last run, then yt_optimize each one (safe mode first pass), "
            "verify the read-back, and report what changed. If nothing is new, reply NO_REPLY."
        ),
        slots=[
            {"name": "time", "type": "time", "label": "What time daily?", "default": "21:00"},
            {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
             "options": ["origin", "local"], "strict": False},
        ],
        tags=["youtube", "media", "daily"],
    )

    ctx.register_automation_blueprint(
        "catalog-sweep",
        title="Tightbeam catalog sweep",
        description="Work through your back catalog night by night under a quota budget: fill missing tags, upgrade titles on top-view videos, fully resumable.",
        category="media",
        schedule_template="{minute} {hour} * * *",
        prompt_template=(
            "Run the Tightbeam catalog sweep: if no audit exists yet, call yt_audit first. "
            "Then call yt_sweep with budget_updates={budget} and mode={mode}. "
            "Report videos updated, quota spent, and remaining backlog. "
            "If the sweep is complete, say so and suggest removing this job."
        ),
        slots=[
            {"name": "time", "type": "time", "label": "What time nightly?", "default": "23:30"},
            {"name": "budget", "type": "text", "label": "Videos per run (50 quota units each)?", "default": "10"},
            {"name": "mode", "type": "enum", "label": "Sweep mode", "default": "both",
             "options": ["tags", "titles", "both"], "strict": True},
            {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
             "options": ["origin", "local"], "strict": False},
        ],
        tags=["youtube", "media", "daily"],
    )

    # Bundled skill(s)
    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.exists():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
