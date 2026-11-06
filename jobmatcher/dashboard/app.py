from __future__ import annotations

import os
import tempfile
import threading
import time
import csv
import io
import json
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from jobmatcher.parsers.resume_parser import ResumeParser
from jobmatcher.models.company import CompanyConfig
from jobmatcher.crawlers.registry import fetch_all_jobs
from jobmatcher.crawlers.deduplication import deduplicate_jobs
from jobmatcher.ranking.engine import RankingEngine
from jobmatcher.filtering.filters import FilterCriteria, filter_jobs
from jobmatcher.models.job import RankedJob

from jobmatcher.dashboard.state import get_state
from jobmatcher.tracking.store import get_default_store
from jobmatcher.tracking.models import Bookmark, ApplicationStatus

# Create FastAPI app
app = FastAPI(title="JobMatcher Dashboard", version="0.1.0", description="Lightweight dashboard for resume-based job crawling & ranking")

# Bookmark store (persistent)
bookmark_store = get_default_store()

# CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
state = get_state()

# Helper to get template path
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Mount static if exists
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def create_app() -> FastAPI:
    return app


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve dashboard HTML."""
    index_path = TEMPLATE_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), media_type="text/html")
    # Fallback inline HTML if template not found
    return HTMLResponse(content=get_fallback_html(), status_code=200)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0", "supported_ats": ["greenhouse", "lever", "ashby", "generic"]}


@app.get("/api/state")
async def get_state_api():
    base = state.to_dict()
    # Add bookmark info
    try:
        base["bookmarks_count"] = bookmark_store.count()
        base["bookmarks_by_status"] = bookmark_store.count_by_status()
    except Exception:
        base["bookmarks_count"] = 0
        base["bookmarks_by_status"] = {}
    return base


@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...)):
    """Upload and parse resume."""
    state.set_status("uploading", f"Parsing resume {file.filename}")

    try:
        # Save temp file
        suffix = Path(file.filename).suffix.lower() or ".txt"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        parser = ResumeParser()
        profile = parser.parse_file(tmp_path)

        # Cleanup temp
        os.unlink(tmp_path)

        state.set_resume(profile, file.filename)
        state.set_status("idle", f"Resume parsed: {len(profile.skills)} skills found")

        return {
            "success": True,
            "filename": file.filename,
            "profile": profile.to_dict(),
            "skills_count": len(profile.skills),
            "state": state.to_dict(),
        }

    except Exception as e:
        state.set_status("error", f"Failed to parse resume: {str(e)}", error=str(e))
        raise HTTPException(status_code=400, detail=f"Failed to parse resume: {e}")


@app.post("/api/companies/upload")
async def upload_companies(file: UploadFile = File(...)):
    """Upload companies JSON/YAML file."""
    state.set_status("uploading", f"Parsing companies {file.filename}")

    try:
        content = await file.read()
        text = content.decode("utf-8")

        # Try to detect format by suffix
        suffix = Path(file.filename).suffix.lower()
        companies: List[CompanyConfig] = []

        if suffix in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text)
            if isinstance(data, dict) and "companies" in data:
                data = data["companies"]
            companies = [CompanyConfig.from_dict(item) for item in data]
        else:
            # JSON
            data = json.loads(text)
            if isinstance(data, dict) and "companies" in data:
                data = data["companies"]
            companies = [CompanyConfig.from_dict(item) for item in data]

        state.set_companies(companies, source=file.filename)
        state.set_status("idle", f"Loaded {len(companies)} companies")

        return {
            "success": True,
            "count": len(companies),
            "companies": [{"name": c.name, "ats": c.ats, "identifier": c.identifier} for c in companies],
            "state": state.to_dict(),
        }

    except Exception as e:
        state.set_status("error", f"Failed to parse companies: {str(e)}", error=str(e))
        raise HTTPException(status_code=400, detail=f"Failed to parse companies: {e}")


@app.post("/api/companies/json")
async def set_companies_json(payload: Dict[str, Any]):
    """Set companies from JSON payload: {"companies": [...]}"""
    try:
        data = payload.get("companies", payload)
        if isinstance(data, dict) and "companies" in data:
            data = data["companies"]
        companies = [CompanyConfig.from_dict(item) for item in data]

        state.set_companies(companies, source="json-payload")
        state.set_status("idle", f"Loaded {len(companies)} companies from JSON")

        return {
            "success": True,
            "count": len(companies),
            "companies": [{"name": c.name, "ats": c.ats, "identifier": c.identifier} for c in companies],
            "state": state.to_dict(),
        }
    except Exception as e:
        state.set_status("error", f"Failed to parse companies JSON: {str(e)}", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/crawl")
async def start_crawl(
    background_tasks: BackgroundTasks,
    max_workers: int = Form(8),
    dedup_strategy: str = Form("smart"),
    timeout: int = Form(15),
    retries: int = Form(2),
    min_score: Optional[float] = Form(None),
    remote_only: bool = Form(False),
):
    """Start crawling and ranking in background."""
    if state.resume_profile is None:
        raise HTTPException(status_code=400, detail="No resume uploaded. Please upload resume first.")

    if not state.companies:
        raise HTTPException(status_code=400, detail="No companies configured. Please upload companies file.")

    if state.status == "crawling":
        raise HTTPException(status_code=409, detail="Crawl already in progress")

    # Capture current config snapshot
    companies_snapshot = list(state.companies)
    resume_snapshot = state.resume_profile

    def crawl_task():
        try:
            state.set_status("crawling", f"Starting crawl of {len(companies_snapshot)} companies with {max_workers} workers")
            start = time.time()

            # Fetch
            state.set_status("crawling", f"Fetching jobs from {len(companies_snapshot)} companies...")
            all_jobs = fetch_all_jobs(
                companies_snapshot,
                max_workers=max_workers,
                deduplicate=False,  # We'll dedup manually for stats
                concurrent=True,
                timeout=timeout,
                retries=retries,
            )
            fetched_count = len(all_jobs)

            state.set_status("crawling", f"Fetched {fetched_count} jobs, deduplicating ({dedup_strategy})...")
            if dedup_strategy != "none":
                all_jobs, removed = deduplicate_jobs(all_jobs, strategy=dedup_strategy)
                dedup_removed = removed
            else:
                dedup_removed = 0

            state.set_status("crawling", f"Deduped to {len(all_jobs)} jobs, ranking against resume...")

            # Rank
            engine = RankingEngine()
            ranked = engine.rank_all(resume_snapshot, all_jobs)

            elapsed = time.time() - start
            stats = {
                "total_fetched": fetched_count,
                "dedup_removed": dedup_removed,
                "ranked_count": len(ranked),
                "elapsed_seconds": round(elapsed, 2),
                "max_workers": max_workers,
                "dedup_strategy": dedup_strategy,
            }

            state.set_ranked_jobs(ranked, stats=stats)
            state.set_status("done", f"Crawled {fetched_count} jobs, {len(ranked)} ranked in {elapsed:.1f}s")

        except Exception as e:
            import traceback

            tb = traceback.format_exc()
            state.set_status("error", f"Crawl failed: {str(e)}", error=tb)

    # Start background thread
    thread = threading.Thread(target=crawl_task, daemon=True)
    thread.start()

    return {"success": True, "message": "Crawl started", "companies": len(companies_snapshot), "state": state.to_dict()}


@app.get("/api/jobs")
async def get_jobs(
    search: Optional[str] = Query(None, description="General search across title, company, description"),
    min_score: Optional[float] = Query(None),
    max_score: Optional[float] = Query(None),
    company: Optional[str] = Query(None, description="Company filter substring"),
    location: Optional[str] = Query(None),
    remote_only: bool = Query(False),
    keyword: Optional[str] = Query(None, description="Keyword must be present"),
    title_contains: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="ATS source filter"),
    sort_by: str = Query("score", description="Sort by: score, company, title, location, matched_skills"),
    sort_order: str = Query("desc", description="asc or desc"),
):
    """Get ranked jobs with optional filtering and sorting."""
    # Copy ranked jobs under lock
    with state.lock:
        ranked = list(state.ranked_jobs)

    if not ranked:
        return {"jobs": [], "count": 0, "total": 0, "filters": {}}

    # Apply filters using existing FilterCriteria where possible
    criteria = FilterCriteria(
        min_score=min_score,
        max_score=max_score,
        companies=[company] if company else [],
        locations=[location] if location else [],
        keywords=[keyword] if keyword else [],
        title_contains=[title_contains] if title_contains else [],
        remote_only=remote_only,
        sources=[source] if source else [],
    )

    from jobmatcher.filtering.filters import filter_jobs as filter_jobs_func

    # Only apply if any filter set
    has_filter = any([min_score is not None, max_score is not None, company, location, remote_only, keyword, title_contains, source])
    if has_filter:
        ranked = filter_jobs_func(ranked, criteria)

    # Additional general search
    if search:
        search_lower = search.lower()
        filtered = []
        for rj in ranked:
            searchable = f"{rj.job.title} {rj.job.company} {rj.job.location} {rj.job.description_plain} {rj.explanation} {' '.join(rj.matched_skills)}".lower()
            if search_lower in searchable:
                filtered.append(rj)
        ranked = filtered

    # Sorting
    reverse = sort_order.lower() == "desc"
    if sort_by == "score":
        ranked.sort(key=lambda x: x.score, reverse=reverse)
    elif sort_by == "company":
        ranked.sort(key=lambda x: x.job.company.lower(), reverse=reverse)
    elif sort_by == "title":
        ranked.sort(key=lambda x: x.job.title.lower(), reverse=reverse)
    elif sort_by == "location":
        ranked.sort(key=lambda x: x.job.location.lower(), reverse=reverse)
    elif sort_by == "matched_skills":
        ranked.sort(key=lambda x: len(x.matched_skills), reverse=reverse)
    else:
        ranked.sort(key=lambda x: x.score, reverse=True)

    # Convert to dict
    jobs_dict = [rj.to_dict() for rj in ranked]

    return {
        "jobs": jobs_dict,
        "count": len(jobs_dict),
        "total": len(state.ranked_jobs),
        "filters": {
            "search": search,
            "min_score": min_score,
            "company": company,
            "location": location,
            "remote_only": remote_only,
            "keyword": keyword,
            "title_contains": title_contains,
            "source": source,
            "sort_by": sort_by,
            "sort_order": sort_order,
        },
    }


@app.get("/api/export/csv")
async def export_csv(
    search: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None),
    max_score: Optional[float] = Query(None),
    company: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    remote_only: bool = Query(False),
    keyword: Optional[str] = Query(None),
    title_contains: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    sort_by: str = Query("score"),
    sort_order: str = Query("desc"),
):
    """Export currently filtered view as CSV."""
    # Reuse filtering logic from /api/jobs
    jobs_response = await get_jobs(
        search=search,
        min_score=min_score,
        max_score=max_score,
        company=company,
        location=location,
        remote_only=remote_only,
        keyword=keyword,
        title_contains=title_contains,
        source=source,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    jobs = jobs_response["jobs"]

    # Create CSV in memory
    output = io.StringIO()
    if jobs:
        fieldnames = list(jobs[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for job in jobs:
            writer.writerow(job)
    else:
        # Write header with default fields
        from jobmatcher.exporter.csv_export import DEFAULT_CSV_FIELDS

        writer = csv.DictWriter(output, fieldnames=DEFAULT_CSV_FIELDS)
        writer.writeheader()

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ranked_jobs_export.csv"},
    )


@app.get("/api/export/json")
async def export_json(
    search: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None),
    max_score: Optional[float] = Query(None),
    company: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    remote_only: bool = Query(False),
    keyword: Optional[str] = Query(None),
    title_contains: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    sort_by: str = Query("score"),
    sort_order: str = Query("desc"),
):
    """Export currently filtered view as JSON."""
    jobs_response = await get_jobs(
        search=search,
        min_score=min_score,
        max_score=max_score,
        company=company,
        location=location,
        remote_only=remote_only,
        keyword=keyword,
        title_contains=title_contains,
        source=source,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    jobs = jobs_response["jobs"]

    json_str = json.dumps(jobs, indent=2)

    return StreamingResponse(
        iter([json_str]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ranked_jobs_export.json"},
    )


@app.post("/api/reset")
async def reset_state():
    state.clear()
    return {"success": True, "state": state.to_dict()}


@app.get("/api/example/companies")
async def example_companies():
    """Return example companies list."""
    from pathlib import Path

    example_path = Path(__file__).parent.parent.parent / "examples" / "companies.json"
    if example_path.exists():
        with open(example_path) as f:
            data = json.load(f)
        return data
    return {
        "companies": [
            {"name": "Airbnb", "ats": "greenhouse", "identifier": "airbnb"},
            {"name": "Datadog", "ats": "greenhouse", "identifier": "datadog"},
            {"name": "OpenAI", "ats": "ashby", "identifier": "openai"},
        ]
    }


# ==================== BOOKMARKS / TRACKING API ====================

@app.get("/api/bookmarks")
async def list_bookmarks(
    status: Optional[str] = Query(None, description="Filter by status: Interested, Applied, Interviewing, Offer, Rejected"),
    company: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """List all bookmarked jobs with optional filters. Persistent across restarts."""
    bms = bookmark_store.list_all(status=status, company=company, search=search)
    return {
        "bookmarks": [bm.to_dict() for bm in bms],
        "count": len(bms),
        "counts_by_status": bookmark_store.count_by_status(),
        "total": bookmark_store.count(),
    }


@app.post("/api/bookmarks")
async def create_bookmark(payload: Dict[str, Any]):
    """
    Bookmark a job. Accepts:
    - job_id, company, title, location, url, source, etc.
    - or full job dict
    - status and notes optional
    """
    try:
        # Payload may contain job object directly or nested
        job_data = payload.get("job") or payload.get("job_data") or payload

        status_val = payload.get("status", "Interested")
        notes = payload.get("notes", "")

        # If job_data has id field, use it
        bm = Bookmark.from_job(job_data, status=status_val, notes=notes)

        # Allow overriding from payload
        if payload.get("match_score") is not None:
            bm.match_score = payload.get("match_score")
        if payload.get("explanation"):
            bm.explanation = payload.get("explanation")

        saved = bookmark_store.add(bm)

        return {"success": True, "bookmark": saved.to_dict(), "count": bookmark_store.count()}

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to bookmark: {e}")


@app.get("/api/bookmarks/{bookmark_id}")
async def get_bookmark(bookmark_id: str):
    bm = bookmark_store.get(bookmark_id) or bookmark_store.get_by_job_id(bookmark_id)
    if not bm:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return {"bookmark": bm.to_dict()}


@app.put("/api/bookmarks/{bookmark_id}")
async def update_bookmark(bookmark_id: str, payload: Dict[str, Any]):
    """Update bookmark status and/or notes. Payload: {"status": "...", "notes": "..."}"""
    status_val = payload.get("status")
    notes = payload.get("notes")

    # Validate status if provided
    if status_val and not ApplicationStatus.is_valid(status_val):
        raise HTTPException(status_code=400, detail=f"Invalid status '{status_val}'. Valid: {ApplicationStatus.values()}")

    updated = bookmark_store.update(bookmark_id, status=status_val, notes=notes)

    if not updated:
        raise HTTPException(status_code=404, detail="Bookmark not found")

    return {"success": True, "bookmark": updated.to_dict()}


@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: str):
    success = bookmark_store.remove(bookmark_id)
    if not success:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return {"success": True, "remaining": bookmark_store.count()}


@app.post("/api/bookmarks/{bookmark_id}/status")
async def update_bookmark_status(bookmark_id: str, payload: Dict[str, Any]):
    """Dedicated endpoint to update only status."""
    new_status = payload.get("status")
    if not new_status:
        raise HTTPException(status_code=400, detail="Missing 'status' field")
    if not ApplicationStatus.is_valid(new_status):
        raise HTTPException(status_code=400, detail=f"Invalid status. Valid: {ApplicationStatus.values()}")

    updated = bookmark_store.update(bookmark_id, status=new_status)
    if not updated:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return {"success": True, "bookmark": updated.to_dict()}


@app.get("/api/bookmarks/export/csv")
async def export_bookmarks_csv(
    status: Optional[str] = Query(None),
    company: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    bms = bookmark_store.list_all(status=status, company=company, search=search)

    output = io.StringIO()
    if bms:
        fieldnames = ["id", "job_id", "company", "title", "location", "url", "source", "status", "notes", "match_score", "created_at", "updated_at"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for bm in bms:
            d = bm.to_dict()
            row = {k: d.get(k, "") for k in fieldnames}
            writer.writerow(row)
    else:
        writer = csv.DictWriter(output, fieldnames=["id", "company", "title", "status"])
        writer.writeheader()

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bookmarks_export.csv"},
    )


def get_fallback_html() -> str:
    return """
    <html><body>
    <h1>JobMatcher Dashboard</h1>
    <p>Template file not found. Please check that jobmatcher/dashboard/templates/index.html exists.</p>
    <p>API is running at /api/health</p>
    </body></html>
    """


# For uvicorn to find: jobmatcher.dashboard.app:app
