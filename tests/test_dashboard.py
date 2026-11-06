import json
from pathlib import Path
from fastapi.testclient import TestClient

from jobmatcher.dashboard.app import app
from jobmatcher.dashboard.state import get_state

client = TestClient(app)


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_state_initial():
    get_state().clear()
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert "has_resume" in data
    assert "companies_count" in data


def test_resume_upload_txt():
    get_state().clear()
    sample_resume = Path(__file__).parent.parent / "examples" / "sample_resume.txt"
    assert sample_resume.exists()

    with open(sample_resume, "rb") as f:
        resp = client.post("/api/resume", files={"file": ("sample_resume.txt", f, "text/plain")})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"]
    assert data["skills_count"] > 0

    # Check state
    state_resp = client.get("/api/state")
    assert state_resp.json()["has_resume"] is True


def test_companies_upload_json():
    get_state().clear()

    companies_payload = {
        "companies": [
            {"name": "Airbnb", "ats": "greenhouse", "identifier": "airbnb"},
            {"name": "Datadog", "ats": "greenhouse", "identifier": "datadog"},
        ]
    }

    # Test via JSON endpoint
    resp = client.post("/api/companies/json", json=companies_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2


def test_companies_upload_file():
    get_state().clear()

    example_path = Path(__file__).parent.parent / "examples" / "companies.json"
    with open(example_path, "rb") as f:
        resp = client.post("/api/companies/upload", files={"file": ("companies.json", f, "application/json")})

    assert resp.status_code == 200
    assert resp.json()["count"] > 0


def test_jobs_empty_initial():
    get_state().clear()
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["jobs"] == []


def test_export_csv_empty():
    get_state().clear()
    resp = client.get("/api/export/csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    text = resp.text
    # Should have header
    assert "title" in text.lower() or "id" in text.lower()


def test_dashboard_index():
    resp = client.get("/")
    assert resp.status_code == 200
    # Should contain JobMatcher title
    assert "JobMatcher" in resp.text
    assert "Browse & Filter" in resp.text or "dashboard" in resp.text.lower()


def test_example_companies_endpoint():
    resp = client.get("/api/example/companies")
    assert resp.status_code == 200
    data = resp.json()
    assert "companies" in data
    assert len(data["companies"]) > 0


def test_reset():
    # Set some state then reset
    get_state().clear()
    # Upload dummy company
    client.post("/api/companies/json", json={"companies": [{"name": "Test", "ats": "greenhouse", "identifier": "test"}]})

    resp = client.post("/api/reset")
    assert resp.status_code == 200
    assert resp.json()["state"]["companies_count"] == 0
    assert resp.json()["state"]["has_resume"] is False


# ================= BOOKMARKS API TESTS =================

def test_bookmarks_empty():
    from jobmatcher.tracking.store import get_default_store
    import tempfile

    # Use temp file for isolation
    with tempfile.TemporaryDirectory() as tmpdir:
        from pathlib import Path
        from jobmatcher.dashboard.app import bookmark_store
        # Clear
        bookmark_store.clear_all()

        resp = client.get("/api/bookmarks")
        assert resp.status_code == 200
        data = resp.json()
        assert "bookmarks" in data
        assert data["count"] == 0


def test_bookmarks_create_and_list():
    from jobmatcher.dashboard.app import bookmark_store

    bookmark_store.clear_all()

    job_data = {
        "id": "test-job-123",
        "company": "Acme",
        "title": "Senior Python Engineer",
        "location": "Remote",
        "url": "https://example.com/jobs/123",
        "source": "greenhouse",
        "match_score": 85.5,
    }

    resp = client.post("/api/bookmarks", json={"job": job_data, "status": "Interested", "notes": "Great fit"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"]
    assert data["bookmark"]["company"] == "Acme"
    assert data["bookmark"]["status"] == "Interested"

    # List
    resp = client.get("/api/bookmarks")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    # Filter by status
    resp = client.get("/api/bookmarks?status=Interested")
    assert resp.json()["count"] == 1

    resp = client.get("/api/bookmarks?status=Applied")
    assert resp.json()["count"] == 0


def test_bookmarks_update_status_and_notes():
    from jobmatcher.dashboard.app import bookmark_store

    bookmark_store.clear_all()

    job_data = {"id": "job-456", "company": "OtherCo", "title": "Backend Engineer", "url": "https://example.com/2"}

    resp = client.post("/api/bookmarks", json={"job": job_data, "status": "Interested"})
    assert resp.status_code == 200
    bookmark_id = resp.json()["bookmark"]["id"]

    # Update status
    resp = client.put(f"/api/bookmarks/{bookmark_id}", json={"status": "Applied"})
    assert resp.status_code == 200
    assert resp.json()["bookmark"]["status"] == "Applied"

    # Update notes
    resp = client.put(f"/api/bookmarks/{bookmark_id}", json={"notes": "Applied on 2024-01-15, great team"})
    assert resp.status_code == 200
    assert "2024-01-15" in resp.json()["bookmark"]["notes"]

    # Update both
    resp = client.put(f"/api/bookmarks/{bookmark_id}", json={"status": "Interviewing", "notes": "Interview scheduled"})
    assert resp.json()["bookmark"]["status"] == "Interviewing"
    assert "Interview" in resp.json()["bookmark"]["notes"]


def test_bookmarks_delete():
    from jobmatcher.dashboard.app import bookmark_store

    bookmark_store.clear_all()

    job_data = {"id": "job-del", "company": "ToDelete", "title": "Temp"}

    resp = client.post("/api/bookmarks", json={"job": job_data})
    assert resp.status_code == 200
    bid = resp.json()["bookmark"]["id"]

    # Delete
    resp = client.delete(f"/api/bookmarks/{bid}")
    assert resp.status_code == 200
    assert resp.json()["success"]

    # Verify gone
    resp = client.get("/api/bookmarks")
    assert resp.json()["count"] == 0


def test_bookmarks_export_csv():
    from jobmatcher.dashboard.app import bookmark_store

    bookmark_store.clear_all()

    client.post("/api/bookmarks", json={"job": {"id": "1", "company": "Acme", "title": "Engineer"}, "status": "Interested"})
    client.post("/api/bookmarks", json={"job": {"id": "2", "company": "Other", "title": "Designer"}, "status": "Applied"})

    resp = client.get("/api/bookmarks/export/csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "Acme" in resp.text
    assert "Other" in resp.text

    # Filtered export
    resp = client.get("/api/bookmarks/export/csv?status=Interested")
    assert resp.status_code == 200
    assert "Acme" in resp.text
    assert "Other" not in resp.text


def test_bookmarks_persistence():
    """Ensure bookmarks persist via file."""
    from jobmatcher.dashboard.app import bookmark_store
    from pathlib import Path
    import tempfile

    # The store already persists to file, test that file exists and reload works
    bookmark_store.clear_all()
    client.post("/api/bookmarks", json={"job": {"id": "persist-1", "company": "PersistCo", "title": "Test"}, "notes": "Should persist"})

    # Check file exists at default location
    file_path = bookmark_store.file_path
    assert file_path.exists()

    # Read raw file
    with open(file_path, "r") as f:
        import json

        data = json.load(f)
        assert len(data) >= 1
        # Find our bookmark
        found = any(item.get("company") == "PersistCo" for item in data)
        assert found

    # Simulate restart by creating new store instance from same file
    from jobmatcher.tracking.store import BookmarkStore

    new_store = BookmarkStore(file_path=file_path)
    assert new_store.count() >= 1
    bms = new_store.list_all(company="PersistCo")
    assert len(bms) == 1
    assert bms[0].notes == "Should persist"

    # Cleanup
    bookmark_store.clear_all()
