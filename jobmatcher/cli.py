from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich.logging import RichHandler

from jobmatcher.models.company import CompanyConfig
from jobmatcher.parsers.resume_parser import ResumeParser
from jobmatcher.crawlers.registry import CrawlerRegistry, fetch_all_jobs, list_supported_ats
from jobmatcher.crawlers.deduplication import deduplicate_jobs, deduplicate_ranked_jobs
from jobmatcher.ranking.engine import RankingEngine, RankingWeights
from jobmatcher.filtering.filters import FilterCriteria, filter_jobs
from jobmatcher.exporter.csv_export import export_to_csv, export_to_json

console = Console()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_time=False, rich_tracebacks=True)],
)
logger = logging.getLogger("jobmatcher")


def load_companies(config_path: Path) -> List[CompanyConfig]:
    if not config_path.exists():
        raise FileNotFoundError(f"Companies config not found: {config_path}")

    suffix = config_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            return CompanyConfig.load_list_from_yaml(str(config_path))
        except ImportError:
            raise RuntimeError("PyYAML required for YAML config. Install via pip install pyyaml or use JSON")
    else:
        return CompanyConfig.load_list_from_json(str(config_path))


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def cli(verbose: bool):
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("jobmatcher").setLevel(logging.DEBUG)


@cli.command()
@click.option("--resume", "-r", required=True, type=click.Path(exists=True), help="Path to resume file (PDF/DOCX/TXT)")
@click.option("--companies", "-c", required=True, type=click.Path(exists=True), help="Path to companies JSON/YAML config")
@click.option("--output", "-o", default="ranked_jobs.csv", type=click.Path(), help="Output CSV path")
@click.option("--json-output", default=None, type=click.Path(), help="Optional JSON output path")
@click.option("--min-score", default=0.0, type=float, help="Minimum match score filter (0-100)")
@click.option("--remote-only", is_flag=True, help="Filter remote only")
@click.option("--keyword", "-k", multiple=True, help="Filter: must contain keyword (can repeat)")
@click.option("--location", "-l", multiple=True, help="Filter: location substring (can repeat)")
@click.option("--company-filter", multiple=True, help="Filter: company name substring (can repeat)")
@click.option("--title-contains", multiple=True, help="Filter: title must contain (can repeat)")
@click.option("--top", default=None, type=int, help="Show only top N results")
@click.option("--timeout", default=15, type=int, help="Request timeout seconds")
@click.option("--max-workers", default=8, type=int, help="Max concurrent workers for crawling (default 8)")
@click.option("--concurrent/--no-concurrent", default=True, help="Enable/disable concurrent crawling")
@click.option(
    "--dedup-strategy",
    type=click.Choice(["smart", "strict", "url", "aggressive", "none"]),
    default="smart",
    help="Deduplication strategy: smart (default, cross-ATS aware), strict, url, aggressive, none",
)
@click.option("--retries", default=2, type=int, help="Retry count per ATS request (kept intact)")
def run(
    resume: str,
    companies: str,
    output: str,
    json_output: Optional[str],
    min_score: float,
    remote_only: bool,
    keyword: tuple,
    location: tuple,
    company_filter: tuple,
    title_contains: tuple,
    top: Optional[int],
    timeout: int,
    max_workers: int,
    concurrent: bool,
    dedup_strategy: str,
    retries: int,
):
    """
    Main workflow: Parse resume -> Concurrently crawl jobs -> Deduplicate -> Rank -> Filter -> Export
    """
    console.print("[bold cyan]JobMatcher[/] - Resume-based job crawler & ranker [dim](concurrent + dedup enabled)[/dim]")
    console.print(f"Supported ATS: {', '.join(list_supported_ats())}")
    console.print(f"Config: workers={max_workers}, concurrent={concurrent}, dedup={dedup_strategy}, retries={retries}")

    # 1. Parse resume
    console.print(f"\n[bold]1. Parsing resume:[/] {resume}")
    parser = ResumeParser()
    try:
        resume_profile = parser.parse_file(resume)
    except Exception as e:
        console.print(f"[red]Failed to parse resume: {e}[/]")
        raise click.Abort()

    console.print(f"  Name: {resume_profile.name or 'N/A'}")
    console.print(f"  Email: {resume_profile.email or 'N/A'}")
    console.print(f"  Skills ({len(resume_profile.skills)}): {', '.join(sorted(resume_profile.skills)[:10])}...")
    console.print(f"  Titles: {resume_profile.titles}")
    console.print(f"  Exp years: {resume_profile.experience_years}")

    # 2. Load companies
    console.print(f"\n[bold]2. Loading companies:[/] {companies}")
    try:
        company_list = load_companies(Path(companies))
    except Exception as e:
        console.print(f"[red]Failed to load companies config: {e}[/]")
        raise click.Abort()

    console.print(f"  Loaded {len(company_list)} companies")
    for comp in company_list[:10]:
        console.print(f"    - {comp.name} ({comp.ats}:{comp.identifier})")
    if len(company_list) > 10:
        console.print(f"    ... and {len(company_list)-10} more")

    # 3. Crawl jobs - now concurrent with dedup
    console.print(f"\n[bold]3. Crawling jobs[/] from {len(company_list)} companies (concurrent={concurrent}, workers={max_workers})...")
    deduplicate_enabled = dedup_strategy != "none"
    strategy_for_dedup = dedup_strategy if deduplicate_enabled else "smart"

    all_jobs = []
    if concurrent and len(company_list) > 1:
        # Manual concurrent with progress bar to show per-company progress
        from jobmatcher.crawlers.registry import _fetch_single_company

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Crawling {len(company_list)} companies...", total=len(company_list))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="crawler") as executor:
                future_to_company = {
                    executor.submit(
                        _fetch_single_company,
                        company,
                        {"timeout": timeout, "retries": retries},
                    ): company
                    for company in company_list
                }

                for future in as_completed(future_to_company):
                    company = future_to_company[future]
                    try:
                        comp, jobs, error = future.result()
                        if error is None:
                            all_jobs.extend(jobs)
                            progress.console.print(f"[green]✓ {comp.name}: {len(jobs)} jobs[/]")
                        else:
                            progress.console.print(f"[yellow]⚠ {company.name} failed: {error}[/]")
                    except Exception as e:
                        progress.console.print(f"[red]✗ {company.name} unexpected error: {e}[/]")
                    progress.advance(task)
    else:
        # Sequential fallback using fetch_all_jobs (keeps existing retry intact)
        all_jobs = fetch_all_jobs(
            company_list,
            max_workers=max_workers,
            deduplicate=False,  # we handle dedup separately for logging
            concurrent=False,
            timeout=timeout,
            retries=retries,
        )

    console.print(f"  Total jobs fetched (pre-dedup): [bold]{len(all_jobs)}[/]")

    # Deduplication step
    if deduplicate_enabled:
        before = len(all_jobs)
        all_jobs, removed = deduplicate_jobs(all_jobs, strategy=strategy_for_dedup)
        console.print(f"  Deduplication [cyan]{dedup_strategy}[/]: removed {removed} duplicates, {len(all_jobs)} remain")
    else:
        console.print(f"  Deduplication skipped (strategy=none)")

    if not all_jobs:
        console.print("[yellow]No jobs found. Check company configs or network.[/]")
        return

    # 4. Rank
    console.print(f"\n[bold]4. Ranking jobs[/] against your profile...")
    engine = RankingEngine()
    ranked = engine.rank_all(resume_profile, all_jobs)
    console.print(f"  Ranked {len(ranked)} jobs")

    # 5. Filter
    criteria = FilterCriteria(
        min_score=min_score if min_score > 0 else None,
        locations=list(location),
        keywords=list(keyword),
        companies=list(company_filter),
        title_contains=list(title_contains),
        remote_only=remote_only,
    )
    has_filter = any(
        [
            min_score > 0,
            location,
            keyword,
            company_filter,
            title_contains,
            remote_only,
        ]
    )
    if has_filter:
        console.print(f"\n[bold]5. Filtering[/] with criteria: {criteria}")
        before = len(ranked)
        ranked = filter_jobs(ranked, criteria)
        console.print(f"  Filtered {before} -> {len(ranked)} jobs")
    else:
        console.print("\n[bold]5. Filtering[/] skipped (no criteria)")

    if top and top > 0:
        ranked = ranked[:top]
        console.print(f"  Top {top} results selected")

    # 6. Display table
    console.print("\n[bold]Results:[/]")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Score", style="cyan", width=6)
    table.add_column("Company", style="green", width=15)
    table.add_column("Title", width=35)
    table.add_column("Location", width=15)
    table.add_column("Source", width=10)
    table.add_column("Matched Skills", width=30)

    for r in ranked[:20]:
        table.add_row(
            f"{r.score:.0f}",
            r.job.company[:15],
            r.job.title[:35],
            r.job.location[:15],
            r.job.source,
            ", ".join(r.matched_skills[:4]),
        )

    console.print(table)
    if len(ranked) > 20:
        console.print(f"... and {len(ranked)-20} more (see CSV)")

    # 7. Export
    console.print(f"\n[bold]6. Exporting to {output}[/]")
    export_to_csv(ranked, output)
    console.print(f"  [green]CSV exported: {output}[/]")

    if json_output:
        export_to_json(ranked, json_output)
        console.print(f"  [green]JSON exported: {json_output}[/]")

    console.print("\n[bold]Top 3 explanations:[/]")
    for r in ranked[:3]:
        console.print(f"[cyan]{r.job.company} - {r.job.title} ({r.score:.0f})[/]")
        console.print(f"  {r.explanation}")
        console.print(f"  URL: {r.job.url}\n")


@cli.command()
@click.option("--companies", "-c", required=True, type=click.Path(), help="Output path for sample companies config")
@click.option("--format", "fmt", type=click.Choice(["json", "yaml"]), default="json", help="Config format")
def init(companies: str, fmt: str):
    """Create a sample companies config file."""

    sample = [
        {"name": "ExampleCo Greenhouse", "ats": "greenhouse", "identifier": "exampleco"},
        {"name": "ExampleCo Lever", "ats": "lever", "identifier": "example"},
        {"name": "ExampleCo Ashby", "ats": "ashby", "identifier": "exampleco"},
    ]

    path = Path(companies)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        with open(path, "w") as f:
            json.dump({"companies": sample}, f, indent=2)
    else:
        try:
            import yaml

            with open(path, "w") as f:
                yaml.dump({"companies": sample}, f, sort_keys=False)
        except ImportError:
            console.print("[red]PyYAML not installed, falling back to JSON[/]")
            with open(path, "w") as f:
                json.dump({"companies": sample}, f, indent=2)

    console.print(f"[green]Sample config created: {path}[/]")
    console.print("Edit this file to add real company board tokens.")
    console.print("Examples:")
    console.print("  - Greenhouse board token: from URL https://boards.greenhouse.io/{token}")
    console.print("  - Lever company: from URL https://jobs.lever.co/{company}")
    console.print("  - Ashby board: from URL https://jobs.ashbyhq.com/{board}")


@cli.command()
@click.option("--resume", "-r", required=True, type=click.Path(exists=True), help="Resume file")
def parse_resume(resume: str):
    """Parse resume and show extracted profile."""
    parser = ResumeParser()
    profile = parser.parse_file(resume)

    console.print(f"[bold]Resume:[/] {resume}")
    console.print(f"Name: {profile.name}")
    console.print(f"Email: {profile.email}")
    console.print(f"Phone: {profile.phone}")
    console.print(f"Experience: {profile.experience_years} years")
    console.print(f"Titles: {profile.titles}")
    console.print(f"Skills ({len(profile.skills)}): {', '.join(sorted(profile.skills))}")
    console.print(f"\nSummary preview: {profile.summary[:300]}")
    console.print(f"\nRaw text length: {len(profile.raw_text)} chars")


@cli.command()
@click.option("--companies", "-c", required=True, type=click.Path(exists=True), help="Companies config")
@click.option("--max-workers", default=8, type=int, help="Max concurrent workers")
@click.option("--concurrent/--no-concurrent", default=True, help="Enable concurrent fetching")
@click.option(
    "--dedup-strategy",
    type=click.Choice(["smart", "strict", "url", "aggressive", "none"]),
    default="smart",
    help="Deduplication strategy",
)
@click.option("--timeout", default=15, type=int)
@click.option("--retries", default=2, type=int)
def crawl(companies: str, max_workers: int, concurrent: bool, dedup_strategy: str, timeout: int, retries: int):
    """Crawl jobs only (no ranking) and show counts - now concurrent with dedup."""
    company_list = load_companies(Path(companies))
    console.print(f"Crawling {len(company_list)} companies (concurrent={concurrent}, workers={max_workers}, dedup={dedup_strategy})")

    deduplicate_enabled = dedup_strategy != "none"
    jobs = fetch_all_jobs(
        company_list,
        max_workers=max_workers,
        deduplicate=deduplicate_enabled,
        dedup_strategy=dedup_strategy if deduplicate_enabled else "smart",
        concurrent=concurrent,
        timeout=timeout,
        retries=retries,
    )

    # For per-company breakdown, we need to run again? Simpler: just show total after dedup
    # But we can also fetch without dedup to show per-company counts if desired
    # Here we show total deduped count, plus note about concurrent
    console.print(f"\n[bold]Total after dedup ({dedup_strategy}): {len(jobs)} jobs[/]")

    # Show sample
    for j in jobs[:10]:
        console.print(f"  {j.company} - {j.title} ({j.location}) [{j.source}]")

    if len(jobs) > 10:
        console.print(f"  ... and {len(jobs)-10} more")


@cli.group()
def bookmarks():
    """Bookmark and track job applications (persistent storage)."""


@bookmarks.command("list")
@click.option("--status", "-s", type=click.Choice(["Interested", "Applied", "Interviewing", "Offer", "Rejected", "Saved", "Archived"], case_sensitive=False), default=None, help="Filter by status")
@click.option("--company", "-c", default=None, help="Filter by company substring")
@click.option("--search", default=None, help="General search across title, company, notes")
@click.option("--output", "-o", default=None, type=click.Path(), help="Optional CSV export path")
def bookmarks_list(status: Optional[str], company: Optional[str], search: Optional[str], output: Optional[str]):
    """List all bookmarked jobs."""
    from jobmatcher.tracking.store import get_default_store

    store = get_default_store()
    bms = store.list_all(status=status, company=company, search=search)

    if not bms:
        console.print("[yellow]No bookmarks found.[/]")
        if status or company or search:
            console.print(f"Filters: status={status}, company={company}, search={search}")
        return

    table = Table(show_header=True, header_style="bold magenta", title=f"Bookmarks ({len(bms)})")
    table.add_column("ID", style="cyan", width=10)
    table.add_column("Company", style="green", width=14)
    table.add_column("Title", width=30)
    table.add_column("Location", width=12)
    table.add_column("Status", width=12)
    table.add_column("Score", width=6)
    table.add_column("Notes", width=25)
    table.add_column("Updated", width=12)

    for bm in bms[:50]:
        status_color = {
            "Interested": "blue",
            "Applied": "yellow",
            "Interviewing": "magenta",
            "Offer": "green",
            "Rejected": "red",
            "Saved": "blue",
            "Archived": "dim",
        }.get(bm.status.value, "white")
        table.add_row(
            bm.id[:8],
            bm.company[:14],
            bm.title[:30],
            bm.location[:12],
            f"[{status_color}]{bm.status.value}[/{status_color}]",
            f"{bm.match_score:.0f}" if bm.match_score else "-",
            (bm.notes[:25] + "...") if len(bm.notes) > 25 else bm.notes,
            bm.updated_at[:10],
        )

    console.print(table)
    if len(bms) > 50:
        console.print(f"... and {len(bms)-50} more (use --output to export all)")

    counts = store.count_by_status()
    console.print(f"\nCounts by status: {counts}")

    if output:
        path = store.export_to_csv(Path(output), bms)
        console.print(f"[green]Exported to {path}[/]")


@bookmarks.command("add")
@click.option("--job-id", required=False, help="Original job ID (or bookmark ID to update)")
@click.option("--company", required=False, help="Company name")
@click.option("--title", required=False, help="Job title")
@click.option("--location", default="", help="Location")
@click.option("--url", default="", help="Job URL")
@click.option("--source", default="", help="ATS source")
@click.option("--status", "-s", type=click.Choice(["Interested", "Applied", "Interviewing", "Offer", "Rejected", "Saved", "Archived"], case_sensitive=False), default="Interested", help="Application status")
@click.option("--notes", "-n", default="", help="Personal notes")
@click.option("--from-csv", type=click.Path(exists=True), help="Add bookmarks from ranked_jobs CSV (requires --job-id)")
@click.option("--from-json", type=click.Path(exists=True), help="Add bookmarks from ranked_jobs JSON (bookmarks all or filter by --job-id)")
def bookmarks_add(
    job_id: Optional[str],
    company: Optional[str],
    title: Optional[str],
    location: str,
    url: str,
    source: str,
    status: str,
    notes: str,
    from_csv: Optional[str],
    from_json: Optional[str],
):
    """Add a bookmark manually or from exported files."""
    from jobmatcher.tracking.store import get_default_store
    from jobmatcher.tracking.models import Bookmark
    import csv
    import json as jsonlib

    store = get_default_store()

    # Bulk from CSV
    if from_csv:
        added = 0
        with open(from_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if job_id and row.get("id") != job_id:
                    continue
                bm = Bookmark.from_dict(
                    {
                        "job_id": row.get("id"),
                        "company": row.get("company"),
                        "title": row.get("title"),
                        "location": row.get("location"),
                        "url": row.get("url"),
                        "source": row.get("source"),
                        "match_score": row.get("match_score"),
                        "explanation": row.get("explanation"),
                    }
                )
                bm.status = status
                bm.notes = notes
                store.add(bm)
                added += 1
        console.print(f"[green]Added {added} bookmarks from {from_csv}[/]")
        return

    # Bulk from JSON
    if from_json:
        added = 0
        with open(from_json, encoding="utf-8") as f:
            data = jsonlib.load(f)
            if isinstance(data, dict) and "bookmarks" in data:
                data = data["bookmarks"]
            for item in data:
                if job_id and item.get("id") != job_id and item.get("job_id") != job_id:
                    continue
                bm = Bookmark.from_dict(item)
                # Override status/notes if provided
                if status:
                    bm.status = status
                if notes:
                    bm.notes = notes
                store.add(bm)
                added += 1
        console.print(f"[green]Added {added} bookmarks from {from_json}[/]")
        return

    # Manual single add
    if not company or not title:
        console.print("[red]For manual add, --company and --title are required (or use --from-csv/--from-json)[/]")
        console.print("Example: jobmatcher bookmarks add --company Airbnb --title 'Senior SWE' --url https://... --status Interested --notes 'Great team'")
        raise click.Abort()

    bm = Bookmark(
        job_id=job_id or "",
        company=company,
        title=title,
        location=location,
        url=url,
        source=source,
        notes=notes,
    )
    # Set status via normalize
    from jobmatcher.tracking.models import ApplicationStatus

    bm.status = ApplicationStatus.normalize(status)
    bm = store.add(bm)
    console.print(f"[green]Bookmarked {bm.company} - {bm.title} [{bm.status.value}] ID={bm.id}[/]")
    console.print(f"Stored at {store.file_path}")


@bookmarks.command("update")
@click.argument("bookmark_id")
@click.option("--status", "-s", type=click.Choice(["Interested", "Applied", "Interviewing", "Offer", "Rejected", "Saved", "Archived"], case_sensitive=False), default=None, help="New status")
@click.option("--notes", "-n", default=None, help="New notes (replaces existing)")
@click.option("--append-notes", default=None, help="Append to existing notes")
def bookmarks_update(bookmark_id: str, status: Optional[str], notes: Optional[str], append_notes: Optional[str]):
    """Update status and/or notes of a bookmarked job."""
    from jobmatcher.tracking.store import get_default_store

    store = get_default_store()
    bm = store.get(bookmark_id) or store.get_by_job_id(bookmark_id)
    if not bm:
        console.print(f"[red]Bookmark not found: {bookmark_id}[/]")
        raise click.Abort()

    new_notes = notes
    if append_notes:
        new_notes = (bm.notes + "\n" + append_notes) if bm.notes else append_notes

    updated = store.update(bookmark_id, status=status, notes=new_notes)
    if updated:
        console.print(f"[green]Updated {updated.id}: {updated.company} - {updated.title} -> {updated.status.value}[/]")
        if updated.notes:
            console.print(f"Notes: {updated.notes[:100]}")
    else:
        console.print("[red]Update failed[/]")


@bookmarks.command("remove")
@click.argument("bookmark_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def bookmarks_remove(bookmark_id: str, yes: bool):
    """Remove a bookmark."""
    from jobmatcher.tracking.store import get_default_store

    store = get_default_store()
    bm = store.get(bookmark_id) or store.get_by_job_id(bookmark_id)
    if not bm:
        console.print(f"[red]Bookmark not found: {bookmark_id}[/]")
        raise click.Abort()

    if not yes:
        if not click.confirm(f"Remove bookmark {bm.company} - {bm.title} [{bm.status.value}]?"):
            console.print("Aborted")
            return

    if store.remove(bookmark_id):
        console.print(f"[green]Removed bookmark {bookmark_id}[/]")
    else:
        console.print("[red]Failed to remove[/]")


@bookmarks.command("export")
@click.option("--output", "-o", default="bookmarks.csv", type=click.Path(), help="Output CSV path")
@click.option("--status", "-s", default=None, help="Filter by status before export")
def bookmarks_export(output: str, status: Optional[str]):
    """Export bookmarks to CSV."""
    from jobmatcher.tracking.store import get_default_store

    store = get_default_store()
    bms = store.list_all(status=status)
    path = store.export_to_csv(Path(output), bms)
    console.print(f"[green]Exported {len(bms)} bookmarks to {path}[/]")
    counts = store.count_by_status()
    console.print(f"Counts: {counts}")


@bookmarks.command("clear")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def bookmarks_clear(yes: bool):
    """Clear ALL bookmarks (use with care)."""
    from jobmatcher.tracking.store import get_default_store

    store = get_default_store()
    count = store.count()
    if count == 0:
        console.print("No bookmarks to clear")
        return

    if not yes:
        if not click.confirm(f"Clear all {count} bookmarks? This cannot be undone!"):
            console.print("Aborted")
            return

    cleared = store.clear_all()
    console.print(f"[green]Cleared {cleared} bookmarks[/]")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind dashboard")
@click.option("--port", default=8000, type=int, help="Port for dashboard")
@click.option("--reload", is_flag=True, help="Enable auto-reload (dev)")
def dashboard(host: str, port: int, reload: bool):
    """Launch lightweight web dashboard to crawl, browse, filter, and export jobs."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Dashboard dependencies not installed.[/]")
        console.print("Install with: pip install -e \".[dashboard]\"")
        raise click.Abort()

    console.print(f"[bold cyan]Starting JobMatcher Dashboard[/] at http://{host}:{port}")
    console.print("Features: upload resume, set companies, concurrent crawl + dedup, search/filter/sort, open postings, export CSV/JSON")
    console.print(f"Supported ATS: {', '.join(list_supported_ats())}")
    console.print("Press Ctrl+C to stop")

    # Import here to avoid hard dependency
    from jobmatcher.dashboard.app import app

    uvicorn.run(app, host=host, port=port, reload=reload)


def main():
    cli()


if __name__ == "__main__":
    main()
