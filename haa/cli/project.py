"""``haa project`` sub-app — the Project outer-framework CLI.

Commands:
    haa project create <brief.yaml> [--seek N] [--output K]
    haa project start <id>
    haa project list
    haa project status <id>
    haa project approve <id> --precursor <campaign_id>
    haa project rework-p1 <id> [--brief <new.yaml>] [--seek N] [--output K]
    haa project abandon <id> [--reason]
    haa project recover <id> [--yes]
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from haa.config import load_config
from haa.models import Brief, ProjectHyperparams, ProjectStatus
from haa.observability import setup_logging
from haa.state import StateStore

project_app = typer.Typer(
    name="project",
    help="Project outer-framework: create / start / review / manage research projects.",
    no_args_is_help=True,
)
console = Console()


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _store() -> StateStore:
    cfg = load_config()
    db_path = cfg.storage.resolved_db_path(cfg.project_root)
    return StateStore(str(db_path))


def _load_brief(path: Path) -> Brief:
    """v1.0.2：统一到 haa.brief_io.load_brief（消灭与 main.py 的重复，
    并获得 .md 自由格式简报的编译路径）。"""
    from haa.brief_io import load_brief

    return load_brief(path)


def _controller(store: StateStore, cfg=None):
    """Build a ProjectController with a real Pipeline (for the LLMClient)."""
    from haa.budget import BudgetManager
    from haa.pipeline import Pipeline
    from haa.project_controller import ProjectController

    cfg = cfg or load_config()
    budget = BudgetManager(store, global_limit=cfg.budget.global_limit)
    pipe = Pipeline(cfg, store, budget)
    return ProjectController(cfg, store, budget, llm=pipe.llm)


def _find_project(store, project_id: str):
    """Prefix-match a project ID; exit(1) if none."""
    project = store.get_project(project_id)
    if project is None:
        # Try prefix match.
        for p in store.list_projects():
            if p.id.startswith(project_id):
                return p
        console.print(f"[red]✗[/red] No project matching '{project_id}'.")
        raise typer.Exit(1)
    return project


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #

@project_app.command(name="create")
def create(
    brief_file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to a brief YAML/JSON file.",
    ),
    seek: int = typer.Option(5, "--seek", help="SEEK candidate count."),
    output: int = typer.Option(3, "--output", help="output_candidate_count cap."),
):
    """Create a Project from a brief file (NOT_STARTED / PR phase)."""
    brief = _load_brief(brief_file)
    cfg = load_config()
    hp = ProjectHyperparams(seek_base_count=seek, output_candidate_count=output)
    with _store() as store:
        ctrl = _controller(store, cfg)
        project = ctrl.create_project(brief, hp)
    console.print(f"[green]✓[/green] Project created: [bold]{project.id}[/bold]")
    console.print(f"  Title:  {brief.title}")
    console.print(f"  Status: {project.status.value}  Phase: {project.phase.value}")
    console.print(f"  Seek={seek}  Output={output}")


@project_app.command(name="start")
def start(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
):
    """Flip the human switch: NOT_STARTED → P1 batch → ARV (or MORIBUND)."""
    cfg = load_config()
    setup_logging(cfg)
    with _store() as store:
        ctrl = _controller(store, cfg)
        project = _find_project(store, project_id)
        console.print(
            f"[cyan]▶ Starting P1 batch for project {project.id}…[/cyan]"
        )
        result = ctrl.start_project(project.id)
        _print_project_summary(result)


@project_app.command(name="resume-p1")
def resume_p1(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
):
    """Resume an interrupted P1 batch（断点恢复：续跑非终态 worker + 补建剩余候选）."""
    cfg = load_config()
    setup_logging(cfg)
    with _store() as store:
        ctrl = _controller(store, cfg)
        project = _find_project(store, project_id)
        console.print(
            f"[cyan]▶ Resuming P1 batch for project {project.id}…[/cyan]"
        )
        result = ctrl.resume_p1_batch(project.id)
        _print_project_summary(result)


@project_app.command(name="list")
def list_projects():
    """List all projects."""
    with _store() as store:
        projects = store.list_projects()
    if not projects:
        console.print("[dim]No projects yet. Run `haa project create <brief.yaml>`.[/dim]")
        return
    table = Table(title="Projects")
    table.add_column("ID", style="dim", width=12)
    table.add_column("Title", style="bold")
    table.add_column("Status", style="cyan")
    table.add_column("Phase")
    table.add_column("Precursors", justify="right")
    table.add_column("Created", style="dim")
    for p in projects:
        status = p.status.value
        if p.is_terminal:
            style = "green" if status == "completed" else "red"
        elif p.is_moribund:
            style = "yellow"
        else:
            style = "cyan"
        table.add_row(
            p.id[:12],
            p.brief.title[:40],
            f"[{style}]{status}[/{style}]",
            p.phase.value,
            str(len(p.precursors)),
            p.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@project_app.command(name="status")
def status(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
):
    """Show detailed project status."""
    with _store() as store:
        project = _find_project(store, project_id)
        linked = store.list_campaigns_for_project(project.id)
        # Fetch campaign statuses.
        campaigns = []
        for cid, role in linked:
            c = store.get_campaign(cid)
            campaigns.append((cid, role, c.status.value if c else "?"))
    _print_project_detail(project, campaigns)


@project_app.command(name="approve")
def approve(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
    precursor: str = typer.Option(
        ..., "--precursor",
        help="Campaign ID of the selected precursor.",
    ),
):
    """ARV→P2 boundary: select a precursor for P2 (v0.4 records the choice)."""
    with _store() as store:
        ctrl = _controller(store)
        project = _find_project(store, project_id)
        result = ctrl.select_precursor(project.id, precursor)
    console.print(
        f"[green]✓[/green] Selected precursor {precursor} for project {project.id}."
    )
    console.print(
        "[dim](v0.4: P2 not yet implemented; selection recorded.)[/dim]"
    )


@project_app.command(name="rework-p1")
def rework_p1(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
    brief_file: Path = typer.Option(
        None, "--brief", help="Optional replacement brief YAML/JSON file.",
    ),
    seek: int = typer.Option(None, "--seek", help="Override SEEK candidate count."),
    output: int = typer.Option(
        None, "--output", help="Override output_candidate_count cap."
    ),
):
    """ARV → P1 reverse: re-run P1 batch with optional new brief/hyperparams."""
    cfg = load_config()
    setup_logging(cfg)
    new_brief = _load_brief(brief_file) if brief_file else None
    new_hp = None
    if seek is not None or output is not None:
        new_hp = ProjectHyperparams(
            seek_base_count=seek if seek is not None else 5,
            output_candidate_count=output if output is not None else 3,
        )
    with _store() as store:
        ctrl = _controller(store, cfg)
        project = _find_project(store, project_id)
        console.print(f"[cyan]▶ Reworking P1 for project {project.id}…[/cyan]")
        result = ctrl.rework_p1(project.id, new_brief=new_brief, new_hyperparams=new_hp)
        _print_project_summary(result)


@project_app.command(name="advance")
def advance(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
):
    """Advance to the next phase: ARV→P2 / EA→P3 / DONE→COMPLETED."""
    cfg = load_config()
    setup_logging(cfg)
    with _store() as store:
        ctrl = _controller(store, cfg)
        project = _find_project(store, project_id)

        if project.phase.value == "arv":
            console.print("[cyan]▶ Advancing ARV → P2 (experiment execution)…[/cyan]")
            result = ctrl.advance_to_p2(project.id)
        elif project.phase.value == "ea":
            console.print("[cyan]▶ Advancing EA → P3 (paper production)…[/cyan]")
            result = ctrl.advance_to_p3(project.id)
        elif project.phase.value == "done":
            console.print("[cyan]▶ Completing project…[/cyan]")
            result = ctrl.complete_project(project.id)
        else:
            console.print(
                f"[yellow]⚠[/yellow] Project is in phase '{project.phase.value}' — "
                f"no auto-advance from here."
            )
            raise typer.Exit(1)

        _print_project_summary(result)


@project_app.command(name="complete")
def complete(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
):
    """Mark project COMPLETED (only valid post-P3, phase=DONE)."""
    with _store() as store:
        ctrl = _controller(store)
        project = _find_project(store, project_id)
        result = ctrl.complete_project(project.id)
    console.print(f"[green]✓[/green] Project {project.id} COMPLETED.")


@project_app.command(name="abandon")
def abandon(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
    reason: str = typer.Option("", "--reason", "-r", help="Reason for abandonment."),
):
    """ABORTED (user-only terminal transition)."""
    with _store() as store:
        ctrl = _controller(store)
        project = _find_project(store, project_id)
        ctrl.abort_project(project.id, reason=reason)
    console.print(f"[red]✗[/red] Project {project.id} ABORTED.")
    if reason:
        console.print(f"  Reason: {reason}")


@project_app.command(name="recover")
def recover(
    project_id: str = typer.Argument(..., help="Project ID (or prefix)."),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Acknowledge the MORIBUND warning and confirm recovery.",
    ),
):
    """MORIBUND → IN_PROGRESS recovery (requires --yes)."""
    with _store() as store:
        ctrl = _controller(store)
        project = _find_project(store, project_id)
        if project.moribund_history:
            last = project.moribund_history[-1]
            console.print(
                f"[yellow]⚠[/yellow] Last MORIBUND at phase '{last.phase}': {last.reason}"
            )
            if last.diagnostic:
                preview = last.diagnostic[:300]
                console.print(f"  Diagnostic: {preview}…")
        if not yes:
            console.print(
                "[yellow]Pass --yes to confirm recovery.[/yellow]"
            )
            raise typer.Exit(1)
        ctrl.recover_from_moribund(project.id, confirm_warning=True)
    console.print(
        f"[green]✓[/green] Project {project.id} recovered to IN_PROGRESS."
    )


# --------------------------------------------------------------------------- #
#  Display helpers
# --------------------------------------------------------------------------- #

def _print_project_summary(project):
    """One-block summary after start/rework."""
    console.print(f"\n[bold]Project {project.id}[/bold]")
    console.print(f"  Status:  [cyan]{project.status.value}[/cyan]")
    console.print(f"  Phase:   {project.phase.value}")
    console.print(f"  Precursors: {len(project.precursors)}")
    if project.is_moribund:
        console.print(f"  [yellow]⚠ MORIBUND[/yellow]: {project.moribund_reason}")
        if project.moribund_diagnostic:
            console.print(f"  Diagnostic: {project.moribund_diagnostic[:200]}…")
    elif project.precursors:
        console.print("  [green]Ready for ARV — review precursors:[/green]")
        for i, pre in enumerate(project.precursors, 1):
            console.print(
                f"    {i}. [bold]{pre.candidate_title}[/bold] "
                f"(grade={pre.grade or '?'}, campaign={pre.campaign_id[:12]})"
            )
        console.print(
            f"  Run: [dim]haa project approve {project.id} "
            f"--precursor <campaign_id>[/dim]"
        )


def _print_project_detail(project, campaigns):
    """Full detail for `haa project status`."""
    console.print(f"\n[bold]Project {project.id}[/bold]")
    console.print(f"  Title:   {project.brief.title}")
    console.print(f"  Status:  [cyan]{project.status.value}[/cyan]")
    console.print(f"  Phase:   {project.phase.value}")
    console.print(f"  Problem: {project.brief.problem_area}")
    hp = project.hyperparams
    console.print(
        f"  Hyper:   seek={hp.seek_base_count} output={hp.output_candidate_count} "
        f"auto_approve={hp.auto_approve_human_review}"
    )

    # Precursors
    if project.precursors:
        console.print(f"\n[bold]Precursors ({len(project.precursors)})[/bold]")
        pre_table = Table(show_lines=False)
        pre_table.add_column("#", style="dim", width=4)
        pre_table.add_column("Title")
        pre_table.add_column("Grade", style="yellow")
        pre_table.add_column("Campaign", style="dim")
        for i, pre in enumerate(project.precursors, 1):
            mark = " ← selected" if project.selected_precursor_campaign_id == pre.campaign_id else ""
            pre_table.add_row(
                str(i),
                pre.candidate_title[:40] + mark,
                pre.grade or "?",
                pre.campaign_id[:12],
            )
        console.print(pre_table)
    else:
        console.print("\n[dim]No precursors yet.[/dim]")

    # Linked campaigns
    if campaigns:
        console.print(f"\n[bold]Linked Campaigns ({len(campaigns)})[/bold]")
        cam_table = Table(show_lines=False)
        cam_table.add_column("Role", style="cyan")
        cam_table.add_column("Campaign ID", style="dim")
        cam_table.add_column("Status")
        for cid, role, status in campaigns:
            cam_table.add_row(role, cid[:12], status)
        console.print(cam_table)

    # MORIBUND
    if project.is_moribund:
        console.print(f"\n[yellow]⚠ MORIBUND[/yellow]: {project.moribund_reason}")
        if project.moribund_diagnostic:
            console.print(f"\n[bold]Diagnostic[/bold]")
            console.print(project.moribund_diagnostic)
        console.print(
            f"\n[dim]Recover with: haa project recover {project.id} --yes[/dim]"
        )
