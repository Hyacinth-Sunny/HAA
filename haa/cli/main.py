"""HAA command-line interface (typer).

Usage::

    haa init                          # initialise the SQLite database
    haa run  <brief.yaml>             # launch a new campaign from a brief
    haa list                          # list all campaigns
    haa report <campaign_id>          # show campaign detail + latest checkpoint
    haa stats                         # show global budget + database stats
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from haa.config import load_config
from haa.models import Brief, CampaignStatus
from haa.observability import setup_logging
from haa.state import StateStore

app = typer.Typer(
    name="haa",
    help="Hyacinth Automated Analyzer — automated research pipeline.",
    no_args_is_help=True,
)
console = Console()


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _store() -> StateStore:
    """Build a StateStore from the resolved config."""
    cfg = load_config()
    db_path = cfg.storage.resolved_db_path(cfg.project_root)
    return StateStore(str(db_path))


def _load_brief(path: Path) -> Brief:
    """Load a Brief — YAML/JSON directly, free-form Markdown via the compiler.

    (v1.0.2 统一到 haa.brief_io.load_brief；此处保留薄壳兼容既有测试。)
    """
    from haa.brief_io import load_brief

    return load_brief(path)


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #

@app.command()
def init():
    """Initialise the SQLite database and campaigns directory."""
    cfg = load_config()
    db_path = cfg.storage.resolved_db_path(cfg.project_root)
    campaigns_dir = cfg.storage.resolved_campaigns_dir(cfg.project_root)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    campaigns_dir.mkdir(parents=True, exist_ok=True)

    # Opening the store creates the schema if absent.
    with _store() as store:
        pass

    console.print(f"[green]✓[/green] Database initialised at {db_path}")
    console.print(f"[green]✓[/green] Campaigns dir: {campaigns_dir}")


@app.command()
def run(
    brief_file: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to a brief YAML/JSON file.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Create the campaign but do not execute the pipeline.",
    ),
):
    """Launch a new campaign from a brief file."""
    brief = _load_brief(brief_file)
    cfg = load_config()
    setup_logging(cfg)  # ensure per-call token/cost logs are actually visible

    with _store() as store:
        campaign = store.create_campaign(brief, budget_limit=cfg.budget.per_campaign)
        console.print(
            f"[green]✓[/green] Campaign created: "
            f"[bold]{campaign.id}[/bold]  ({campaign.title})"
        )

        if dry_run:
            console.print("[dim]--dry-run: skipping pipeline execution.[/dim]")
            return

        # Lazy-import pipeline + budget to avoid heavy deps on simple commands.
        from haa.budget import BudgetManager
        from haa.pipeline import Pipeline

        budget = BudgetManager(store, global_limit=cfg.budget.global_limit)
        pipe = Pipeline(cfg, store, budget)

        console.print("[cyan]▶ Starting pipeline…[/cyan]")
        result = pipe.run_campaign(campaign.id, brief=brief)

        if result.is_terminal and result.status.value == "published":
            console.print(
                f"[green]✓[/green] Campaign [bold]{result.id}[/bold] PUBLISHED."
            )
        else:
            console.print(
                f"[yellow]⚠[/yellow] Campaign [bold]{result.id}[/bold] "
                f"ended with status {result.status.value}."
            )


@app.command(name="list")
def list_campaigns():
    """List all campaigns."""
    with _store() as store:
        campaigns = store.list_campaigns()

    if not campaigns:
        console.print("[dim]No campaigns yet. Run `haa run <brief.yaml>`.[/dim]")
        return

    table = Table(title="Campaigns", show_lines=False)
    table.add_column("ID", style="dim", width=12)
    table.add_column("Title", style="bold")
    table.add_column("Status", style="cyan")
    table.add_column("Budget", justify="right")
    table.add_column("Created", style="dim")

    for c in campaigns:
        budget_str = f"${c.budget_used:.2f} / ${c.budget_limit:.2f}"
        # Colour-code status.
        status = c.status.value
        if c.is_terminal:
            status_style = "green" if status == "published" else "red"
        else:
            status_style = "yellow"
        table.add_row(
            c.id[:12],
            c.title or "(untitled)",
            f"[{status_style}]{status}[/{status_style}]",
            budget_str,
            c.created_at.strftime("%Y-%m-%d %H:%M"),
        )

    console.print(table)


@app.command()
def report(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)."),
):
    """Show detailed status for a campaign."""
    with _store() as store:
        # Allow prefix match.
        campaigns = store.list_campaigns()
        match = next(
            (c for c in campaigns if c.id.startswith(campaign_id)),
            None,
        )
        if match is None:
            console.print(f"[red]✗[/red] No campaign matching '{campaign_id}'.")
            raise typer.Exit(1)

        candidates = store.list_candidates(match.id)
        checkpoints = store.list_checkpoints(match.id)

    console.print(f"\n[bold]Campaign {match.id}[/bold]")
    console.print(f"  Title:   {match.title}")
    console.print(f"  Status:  [cyan]{match.status.value}[/cyan]")
    console.print(
        f"  Budget:  ${match.budget_used:.2f} / ${match.budget_limit:.2f} "
        f"({match.budget_remaining:.2f} remaining)"
    )
    console.print(f"  Created: {match.created_at.isoformat()}")
    console.print(f"  Updated: {match.updated_at.isoformat()}")

    # Candidates.
    if candidates:
        console.print(f"\n[bold]Candidates ({len(candidates)})[/bold]")
        cand_table = Table(show_lines=False)
        cand_table.add_column("#", style="dim", width=4)
        cand_table.add_column("ID", style="dim", width=12)
        cand_table.add_column("Title")
        cand_table.add_column("Status", style="cyan")
        cand_table.add_column("Grade", style="yellow")
        for i, cand in enumerate(candidates, 1):
            cand_table.add_row(
                str(i),
                cand.id[:12],
                cand.title[:40] + ("…" if len(cand.title) > 40 else ""),
                cand.status.value,
                (cand.grade.value if cand.grade else ""),
            )
        console.print(cand_table)
    else:
        console.print("\n[dim]No candidates yet.[/dim]")

    # Latest checkpoint.
    if checkpoints:
        latest = checkpoints[-1]
        console.print(f"\n[bold]Latest checkpoint[/bold]")
        console.print(f"  Stage:    {latest.stage}")
        console.print(f"  At:       {latest.created_at.isoformat()}")
    else:
        console.print("\n[dim]No checkpoints.[/dim]")


@app.command()
def stats():
    """Show global database and budget statistics."""
    with _store() as store:
        s = store.stats()

    console.print("[bold]Database Statistics[/bold]")
    console.print(f"  Campaigns:  {s.get('total_campaigns', 0)}")
    console.print(f"  Published:  {s.get('published', 0)}")
    by_status = s.get("by_status", {})
    if by_status:
        console.print(f"  By status:  {by_status}")

    total_used = s.get("total_budget_used", 0.0)
    console.print(f"  Total spent:${total_used:.2f}")


# --------------------------------------------------------------------------- #
#  Human gate: approve / reject
# --------------------------------------------------------------------------- #

def _find_campaign(store, campaign_id: str):
    """Prefix-match a campaign ID; exit(1) if none."""
    campaigns = store.list_campaigns()
    match = next((c for c in campaigns if c.id.startswith(campaign_id)), None)
    if match is None:
        console.print(f"[red]✗[/red] No campaign matching '{campaign_id}'.")
        raise typer.Exit(1)
    return match


@app.command()
def approve(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)."),
):
    """人工审核通过：从 WRITE 继续 pipeline。"""
    cfg = load_config()
    setup_logging(cfg)
    with _store() as store:
        c = _find_campaign(store, campaign_id)
        if c.status != CampaignStatus.AWAITING_HUMAN_REVIEW:
            console.print(
                f"[red]✗[/red] Campaign status is {c.status.value}, "
                f"not awaiting_human_review."
            )
            raise typer.Exit(1)
        from haa.budget import BudgetManager
        from haa.pipeline import Pipeline

        budget = BudgetManager(store, global_limit=cfg.budget.global_limit)
        pipe = Pipeline(cfg, store, budget)
        console.print(f"[green]✓[/green] Approving {c.id} — resuming at WRITE…")
        result = pipe.run_campaign(c.id)
        if result.is_terminal and result.status.value == "published":
            console.print(f"[green]✓[/green] Campaign PUBLISHED.")
        else:
            console.print(
                f"[yellow]⚠[/yellow] Campaign ended with status {result.status.value}."
            )


@app.command()
def reject(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)."),
    reason: str = typer.Option("", "--reason", "-r", help="Rejection reason."),
):
    """人工审核拒绝：终止 campaign。"""
    with _store() as store:
        c = _find_campaign(store, campaign_id)
        if c.status != CampaignStatus.AWAITING_HUMAN_REVIEW:
            console.print(
                f"[red]✗[/red] Campaign status is {c.status.value}, "
                f"not awaiting_human_review."
            )
            raise typer.Exit(1)
        c.status = CampaignStatus.RETIRED
        store.save_campaign(c)
        store.save_checkpoint(c.id, f"RETIRED:human_rejected:{reason}", {})
        console.print(f"[red]✗[/red] Campaign {c.id} rejected and retired.")
        if reason:
            console.print(f"  Reason: {reason}")


# --------------------------------------------------------------------------- #
#  Project sub-app (outer framework)
# --------------------------------------------------------------------------- #

from haa.cli.project import project_app  # noqa: E402

app.add_typer(project_app, name="project")


# --------------------------------------------------------------------------- #
#  Memory sub-app (大修 M-b：记忆库实体层)
# --------------------------------------------------------------------------- #

memory_app = typer.Typer(help="Memory bank (entities / lint / derived index).")


@memory_app.command(name="rebuild")
def memory_rebuild(
    root: str = typer.Option("data/memory", help="Memory bank root dir."),
):
    """Rebuild the derived SQLite index from the Markdown master store."""
    from haa.config import _PROJECT_ROOT
    from haa.memory_bank import MemoryBank

    bank = MemoryBank(_PROJECT_ROOT / root)
    n = bank.rebuild_index()
    console.print(f"[green]✓[/green] Rebuilt index: {n} entities → {bank.index_path()}")


@memory_app.command(name="lint")
def memory_lint(
    root: str = typer.Option("data/memory", help="Memory bank root dir."),
):
    """Lint the memory bank (fields / edge pairs / transitions / ids)."""
    from haa.config import _PROJECT_ROOT
    from haa.memory_bank import MemoryBank

    bank = MemoryBank(_PROJECT_ROOT / root)
    problems = bank.lint()
    if problems:
        for p in problems:
            console.print(f"[red]✗[/red] {p}")
        raise typer.Exit(code=1)
    console.print("[green]✓[/green] Memory bank lint clean.")


app.add_typer(memory_app, name="memory")

@memory_app.command(name="rotate-events")
def memory_rotate_events(
    root: str = typer.Option("data/memory", help="Unused; kept for CLI compat."),
):
    """B4: rotate terminal-campaign events to JSON archive files."""
    from haa.state import StateStore
    from haa.event_rotation import rotate_events

    store = StateStore("data/haa.db")
    rotated = 0
    from haa.config import _PROJECT_ROOT
    campaigns_dir = _PROJECT_ROOT / "data" / "campaigns"
    for d in campaigns_dir.iterdir():
        if d.is_dir() and d.name != "_rejected":
            cid = d.name
            camp = store.get_campaign(cid)
            if camp is not None:
                n = rotate_events(store, cid, campaigns_dir=campaigns_dir)
                rotated += n
    console.print(f"[green]✓[/green] Rotated {rotated} events total.")


# --------------------------------------------------------------------------- #
#  Brief preflight sub-app (大修批次4/P1-a：简报质量预检，软门)
# --------------------------------------------------------------------------- #

brief_app = typer.Typer(help="Brief quality preflight (soft gate, non-blocking).")


@brief_app.command(name="preflight")
def brief_preflight_cmd(
    brief_file: str = typer.Argument(..., help="Path to the brief markdown."),
):
    """Run the soft preflight (structure / vagueness / contradictions)."""
    from pathlib import Path

    from haa.brief_preflight import preflight

    md = Path(brief_file).read_text(encoding="utf-8")
    report = preflight(md)
    if report["verdict"] == "clean":
        console.print("[green]✓[/green] Preflight clean.")
    else:
        console.print(f"[yellow]！[/yellow] Preflight: {report['verdict']} "
                      f"(high={report['counts']['high']} "
                      f"medium={report['counts']['medium']} "
                      f"info={report['counts']['info']})")
        for pr in report["problems"]:
            console.print(f"  [{ 'red' if pr['severity']=='high' else 'yellow' }]•[/] "
                          f"[{pr['kind']}] {pr['message']}")
            console.print(f"      ↳ {pr['suggestion']}")


app.add_typer(brief_app, name="brief")


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
