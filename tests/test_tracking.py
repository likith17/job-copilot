import tempfile
from pathlib import Path
import json

from jobmatcher.models.job import Job
from jobmatcher.tracking.models import Bookmark, ApplicationStatus
from jobmatcher.tracking.store import BookmarkStore


def make_job(job_id="job-123", company="Acme", title="Software Engineer", location="Remote", url="https://example.com/jobs/1", source="greenhouse"):
    return Job(id=job_id, title=title, company=company, location=location, url=url, source=source, description="Python job")


def test_application_status_normalize():
    assert ApplicationStatus.normalize("interested") == ApplicationStatus.INTERESTED
    assert ApplicationStatus.normalize("Applied") == ApplicationStatus.APPLIED
    assert ApplicationStatus.normalize("interviewing") == ApplicationStatus.INTERVIEWING
    assert ApplicationStatus.normalize("offer") == ApplicationStatus.OFFER
    assert ApplicationStatus.normalize("rejected") == ApplicationStatus.REJECTED
    assert ApplicationStatus.normalize("saved") == ApplicationStatus.INTERESTED
    assert ApplicationStatus.is_valid("Applied")
    assert not ApplicationStatus.is_valid("InvalidStatus")


def test_bookmark_from_job():
    job = make_job()
    bm = Bookmark.from_job(job, status="Interested", notes="Great fit")
    assert bm.company == "Acme"
    assert bm.title == "Software Engineer"
    assert bm.status == ApplicationStatus.INTERESTED
    assert bm.notes == "Great fit"
    assert bm.job_id == "job-123"


def test_bookmark_to_from_dict():
    job = make_job()
    bm = Bookmark.from_job(job, status="Applied", notes="Applied yesterday")
    d = bm.to_dict()
    bm2 = Bookmark.from_dict(d)
    assert bm2.company == bm.company
    assert bm2.status == bm.status
    assert bm2.notes == bm.notes
    assert bm2.job_id == bm.job_id


def test_bookmark_store_add_and_persist():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        store = BookmarkStore(file_path=file_path)

        assert store.count() == 0

        job = make_job(job_id="1", company="Acme")
        bm = store.add_from_job(job, status="Interested", notes="First")

        assert store.count() == 1
        assert file_path.exists()

        # Load from file in new store instance (persistence across restarts)
        store2 = BookmarkStore(file_path=file_path)
        assert store2.count() == 1
        bms = store2.list_all()
        assert bms[0].company == "Acme"
        assert bms[0].status == ApplicationStatus.INTERESTED
        assert bms[0].notes == "First"


def test_bookmark_store_update_status_and_notes():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        store = BookmarkStore(file_path=file_path)

        job = make_job(job_id="job-1")
        bm = store.add_from_job(job, status="Interested")

        # Update status
        updated = store.update(bm.id, status="Applied")
        assert updated.status == ApplicationStatus.APPLIED

        # Update notes
        updated = store.update(bm.id, notes="Applied on 2024-01-01")
        assert "2024-01-01" in updated.notes

        # Update both
        updated = store.update(bm.id, status="Interviewing", notes="Interview scheduled")
        assert updated.status == ApplicationStatus.INTERVIEWING
        assert "Interview" in updated.notes

        # Verify persistence
        store2 = BookmarkStore(file_path=file_path)
        bms = store2.list_all()
        assert bms[0].status == ApplicationStatus.INTERVIEWING
        assert "Interview" in bms[0].notes


def test_bookmark_store_duplicate_job_id_handling():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        store = BookmarkStore(file_path=file_path)

        job = make_job(job_id="dup-1", company="Acme", title="Engineer")
        bm1 = store.add_from_job(job, status="Interested", notes="First note")
        assert store.count() == 1
        first_id = bm1.id
        first_created = bm1.created_at

        # Add same job_id again with different status - should update existing, not duplicate
        job2 = make_job(job_id="dup-1", company="Acme", title="Engineer")
        bm2 = store.add_from_job(job2, status="Applied", notes="Second note")

        assert store.count() == 1  # Still 1, not duplicated
        assert bm2.id == first_id  # Same bookmark id preserved
        assert bm2.status == ApplicationStatus.APPLIED
        assert bm2.created_at == first_created  # Created_at preserved


def test_bookmark_store_remove():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        store = BookmarkStore(file_path=file_path)

        job1 = make_job(job_id="1", company="Acme")
        job2 = make_job(job_id="2", company="OtherCo")

        store.add_from_job(job1)
        store.add_from_job(job2)
        assert store.count() == 2

        # Remove by bookmark id
        bms = store.list_all()
        to_remove = bms[0]
        assert store.remove(to_remove.id) is True
        assert store.count() == 1

        # Remove by job_id
        remaining = store.list_all()[0]
        assert store.remove(remaining.job_id) is True
        assert store.count() == 0

        # Verify file persistence
        store2 = BookmarkStore(file_path=file_path)
        assert store2.count() == 0


def test_bookmark_store_filtering():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        store = BookmarkStore(file_path=file_path)

        store.add_from_job(make_job(job_id="1", company="Acme", title="Python Engineer"), status="Interested")
        store.add_from_job(make_job(job_id="2", company="Acme", title="Java Engineer"), status="Applied")
        store.add_from_job(make_job(job_id="3", company="OtherCo", title="Python Engineer"), status="Interviewing")

        # Filter by status
        interested = store.list_all(status="Interested")
        assert len(interested) == 1
        assert interested[0].company == "Acme"

        # Filter by company
        acme = store.list_all(company="Acme")
        assert len(acme) == 2

        # Filter by search
        python = store.list_all(search="Python")
        assert len(python) == 2

        # Combined filter
        acme_python = store.list_all(company="Acme", search="Python")
        assert len(acme_python) == 1


def test_bookmark_store_count_by_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        store = BookmarkStore(file_path=file_path)

        store.add_from_job(make_job(job_id="1"), status="Interested")
        store.add_from_job(make_job(job_id="2"), status="Applied")
        store.add_from_job(make_job(job_id="3"), status="Applied")
        store.add_from_job(make_job(job_id="4"), status="Offer")

        counts = store.count_by_status()
        assert counts["Interested"] == 1
        assert counts["Applied"] == 2
        assert counts["Offer"] == 1


def test_bookmark_store_export_csv():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        export_path = Path(tmpdir) / "export.csv"
        store = BookmarkStore(file_path=file_path)

        store.add_from_job(make_job(job_id="1", company="Acme"), status="Interested", notes="Test note")

        result_path = store.export_to_csv(export_path)
        assert result_path.exists()
        content = result_path.read_text()
        assert "Acme" in content
        assert "Interested" in content
        assert "Test note" in content


def test_bookmark_persistence_across_restarts():
    """Core requirement: bookmarks persist after restarting application."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"

        # Simulate first app run - add bookmarks
        store1 = BookmarkStore(file_path=file_path)
        store1.add_from_job(make_job(job_id="1", company="Acme"), status="Interested", notes="First run")
        store1.add_from_job(make_job(job_id="2", company="Other"), status="Applied", notes="Applied")
        assert store1.count() == 2

        # Simulate app restart - new store instance loads from same file
        store2 = BookmarkStore(file_path=file_path)
        assert store2.count() == 2

        # Verify data integrity
        bms = store2.list_all()
        companies = {bm.company for bm in bms}
        assert "Acme" in companies
        assert "Other" in companies

        # Simulate updating in second run and restarting again
        acme_bm = [bm for bm in bms if bm.company == "Acme"][0]
        store2.update(acme_bm.id, status="Interviewing", notes="Interview scheduled after restart")

        store3 = BookmarkStore(file_path=file_path)
        acme_after = store3.get(acme_bm.id)
        assert acme_after.status == ApplicationStatus.INTERVIEWING
        assert "Interview scheduled" in acme_after.notes


def test_bookmark_from_ranked_job():
    from jobmatcher.models.job import RankedJob

    job = make_job(job_id="ranked-1", company="Acme", title="Senior Python Engineer")
    ranked = RankedJob(job=job, score=85.5, explanation="Great match", matched_skills=["Python"])

    bm = Bookmark.from_job(ranked, status="Interested", notes="High score")
    assert bm.match_score == 85.5
    assert "Great match" in bm.explanation
    assert bm.company == "Acme"


def test_bookmark_store_thread_safety():
    """Test that concurrent adds don't corrupt file."""
    import threading

    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bookmarks.json"
        store = BookmarkStore(file_path=file_path)

        def add_jobs(start, end):
            for i in range(start, end):
                job = make_job(job_id=f"job-{i}", company=f"Company{i}", title=f"Engineer {i}")
                store.add_from_job(job, status="Interested")

        threads = []
        for t in range(3):
            thread = threading.Thread(target=add_jobs, args=(t * 10, (t + 1) * 10))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Should have 30 bookmarks, no corruption
        assert store.count() == 30

        # Reload and verify
        store2 = BookmarkStore(file_path=file_path)
        assert store2.count() == 30
