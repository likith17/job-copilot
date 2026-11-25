from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import List, Optional, Dict
import logging

from jobmatcher.tracking.models import Bookmark, ApplicationStatus

logger = logging.getLogger(__name__)


class BookmarkStore:
    """Thread-safe JSON file store for bookmarks with persistence across restarts."""

    def __init__(self, file_path: Optional[Path] = None):
        """
        Args:
            file_path: Path to JSON file for persistence. If None, uses default location:
                       - Env var JOBMATCHER_BOOKMARKS_FILE if set
                       - Otherwise ~/.jobmatcher/bookmarks.json
                       - Fallback to ./bookmarks.json if home not writable
        """
        if file_path is None:
            file_path = self._default_path()

        self.file_path = Path(file_path)
        self._lock = threading.RLock()
        self._bookmarks: Dict[str, Bookmark] = {}  # id -> Bookmark
        self._ensure_dir()
        self.load()

    @staticmethod
    def _default_path() -> Path:
        # Check env var
        env_path = os.getenv("JOBMATCHER_BOOKMARKS_FILE")
        if env_path:
            return Path(env_path)

        # Try ~/.jobmatcher/bookmarks.json
        try:
            home_path = Path.home() / ".jobmatcher" / "bookmarks.json"
            # Ensure parent exists or can be created
            home_path.parent.mkdir(parents=True, exist_ok=True)
            # Test writable
            if os.access(home_path.parent, os.W_OK):
                return home_path
        except Exception:
            pass

        # Fallback to ./data/bookmarks.json or ./bookmarks.json
        for fallback in [Path("./data/bookmarks.json"), Path("./bookmarks.json")]:
            try:
                fallback.parent.mkdir(parents=True, exist_ok=True)
                return fallback
            except Exception:
                continue

        # Final fallback
        return Path("./bookmarks.json")

    def _ensure_dir(self):
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create bookmarks dir {self.file_path.parent}: {e}")

    def load(self) -> int:
        """Load bookmarks from file. Returns count loaded."""
        with self._lock:
            if not self.file_path.exists():
                self._bookmarks = {}
                return 0

            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Support both list and dict formats
                if isinstance(data, dict) and "bookmarks" in data:
                    items = data["bookmarks"]
                elif isinstance(data, list):
                    items = data
                else:
                    items = []

                self._bookmarks = {}
                for item in items:
                    try:
                        bm = Bookmark.from_dict(item)
                        self._bookmarks[bm.id] = bm
                    except Exception as e:
                        logger.warning(f"Skipping invalid bookmark entry: {e}")
                        continue

                logger.info(f"Loaded {len(self._bookmarks)} bookmarks from {self.file_path}")
                return len(self._bookmarks)

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse bookmarks file {self.file_path}: {e}")
                # Backup corrupted file
                try:
                    backup = self.file_path.with_suffix(".json.corrupted")
                    self.file_path.rename(backup)
                    logger.info(f"Backed up corrupted file to {backup}")
                except Exception:
                    pass
                self._bookmarks = {}
                return 0
            except Exception as e:
                logger.error(f"Failed to load bookmarks: {e}")
                self._bookmarks = {}
                return 0

    def save(self) -> bool:
        """Persist bookmarks to file. Returns success."""
        with self._lock:
            try:
                self._ensure_dir()
                # Write to temp file then rename for atomicity
                temp_path = self.file_path.with_suffix(".tmp")

                data = [bm.to_dict() for bm in self._bookmarks.values()]

                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

                # Atomic replace
                temp_path.replace(self.file_path)
                logger.debug(f"Saved {len(self._bookmarks)} bookmarks to {self.file_path}")
                return True

            except Exception as e:
                logger.error(f"Failed to save bookmarks to {self.file_path}: {e}")
                return False

    def add(self, bookmark: Bookmark) -> Bookmark:
        """Add or update bookmark. If job_id exists, updates existing."""
        with self._lock:
            # Check if bookmark with same job_id already exists - update instead of duplicate
            # But keep bookmark id if exists
            existing_by_job_id = None
            for bm in self._bookmarks.values():
                if bm.job_id and bm.job_id == bookmark.job_id and bm.company == bookmark.company:
                    existing_by_job_id = bm
                    break

            if existing_by_job_id:
                # Update existing
                bookmark.id = existing_by_job_id.id
                # Preserve created_at
                bookmark.created_at = existing_by_job_id.created_at
                # Preserve status/notes if not provided? Currently override only if new values are non-default?
                # For explicit add, we will override with provided status/notes
                bookmark.update_timestamp()
                self._bookmarks[bookmark.id] = bookmark
            else:
                # New bookmark
                # Ensure id unique
                if bookmark.id in self._bookmarks:
                    import uuid

                    bookmark.id = str(uuid.uuid4())[:12]
                self._bookmarks[bookmark.id] = bookmark

            self.save()
            return self._bookmarks[bookmark.id]

    def add_from_job(self, job, status: str = "Interested", notes: str = "", match_score: Optional[float] = None) -> Bookmark:
        """Convenience to add from Job/RankedJob/dict."""
        bm = Bookmark.from_job(job, status=status, notes=notes, match_score=match_score)
        return self.add(bm)

    def get(self, bookmark_id: str) -> Optional[Bookmark]:
        with self._lock:
            return self._bookmarks.get(bookmark_id)

    def get_by_job_id(self, job_id: str) -> Optional[Bookmark]:
        with self._lock:
            for bm in self._bookmarks.values():
                if bm.job_id == job_id:
                    return bm
            return None

    def list_all(self, status: Optional[str] = None, company: Optional[str] = None, search: Optional[str] = None) -> List[Bookmark]:
        """List bookmarks with optional filters."""
        with self._lock:
            results = list(self._bookmarks.values())

        if status:
            try:
                norm_status = ApplicationStatus.normalize(status)
                results = [bm for bm in results if bm.status == norm_status]
            except Exception:
                # If status invalid, filter by string contains
                results = [bm for bm in results if status.lower() in bm.status.value.lower()]

        if company:
            comp_lower = company.lower()
            results = [bm for bm in results if comp_lower in bm.company.lower()]

        if search:
            search_lower = search.lower()
            results = [
                bm
                for bm in results
                if search_lower in bm.title.lower()
                or search_lower in bm.company.lower()
                or search_lower in bm.location.lower()
                or search_lower in bm.notes.lower()
                or search_lower in bm.status.value.lower()
            ]

        # Sort by updated_at desc (most recent first)
        results.sort(key=lambda x: x.updated_at, reverse=True)
        return results

    def update(self, bookmark_id: str, status: Optional[str] = None, notes: Optional[str] = None, **extra) -> Optional[Bookmark]:
        """Update bookmark status and/or notes."""
        with self._lock:
            bm = self._bookmarks.get(bookmark_id)
            if not bm:
                # Try by job_id
                bm = self.get_by_job_id(bookmark_id)
                if not bm:
                    return None

            if status is not None:
                bm.status = ApplicationStatus.normalize(status)

            if notes is not None:
                bm.notes = notes

            # Allow updating other fields via extra
            for k, v in extra.items():
                if hasattr(bm, k) and v is not None:
                    setattr(bm, k, v)

            bm.update_timestamp()
            self._bookmarks[bm.id] = bm
            self.save()
            return bm

    def remove(self, bookmark_id: str) -> bool:
        """Remove bookmark by id or job_id. Returns True if removed."""
        with self._lock:
            if bookmark_id in self._bookmarks:
                del self._bookmarks[bookmark_id]
                self.save()
                return True

            # Try by job_id
            to_delete = None
            for bid, bm in self._bookmarks.items():
                if bm.job_id == bookmark_id:
                    to_delete = bid
                    break

            if to_delete:
                del self._bookmarks[to_delete]
                self.save()
                return True

            return False

    def clear_all(self) -> int:
        """Clear all bookmarks. Returns count cleared."""
        with self._lock:
            count = len(self._bookmarks)
            self._bookmarks.clear()
            self.save()
            return count

    def count(self) -> int:
        with self._lock:
            return len(self._bookmarks)

    def count_by_status(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for bm in self._bookmarks.values():
                status_val = bm.status.value
                counts[status_val] = counts.get(status_val, 0) + 1
            return counts

    def export_to_csv(self, output_path: Path, bookmarks: Optional[List[Bookmark]] = None) -> Path:
        """Export bookmarks to CSV."""
        import csv

        bookmarks = bookmarks or self.list_all()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = ["id", "job_id", "company", "title", "location", "url", "source", "status", "notes", "match_score", "created_at", "updated_at"]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for bm in bookmarks:
                d = bm.to_dict()
                # Only write specified fields
                row = {k: d.get(k, "") for k in fieldnames}
                writer.writerow(row)

        return output_path


# Singleton default store
_default_store: Optional[BookmarkStore] = None
_default_lock = threading.Lock()


def get_default_store(file_path: Optional[Path] = None) -> BookmarkStore:
    global _default_store
    with _default_lock:
        if _default_store is None or (file_path is not None and _default_store.file_path != Path(file_path)):
            _default_store = BookmarkStore(file_path=file_path)
        return _default_store


def reset_default_store():
    """For testing - reset singleton."""
    global _default_store
    with _default_lock:
        _default_store = None
