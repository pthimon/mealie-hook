"""Small on-disk state: the processing cut-off, retry counts, snapshots and run records."""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


class State:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.lock = threading.Lock()
        self.data = {"since": None, "attempts": {}, "done": [], "last_sweep": None}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.replace(self.path)

    def since(self, override: str = "") -> datetime:
        """Recipes created before this are never candidates. Set on first run."""
        with self.lock:
            if override:
                return parse_ts(override)
            if not self.data.get("since"):
                self.data["since"] = now_iso()
                self._save()
            return parse_ts(self.data["since"])

    def is_done(self, slug: str) -> bool:
        return slug in self.data["done"]

    def mark_done(self, slug: str):
        with self.lock:
            if slug not in self.data["done"]:
                self.data["done"].append(slug)
            self.data["attempts"].pop(slug, None)
            self._save()

    def bump_attempt(self, slug: str) -> int:
        with self.lock:
            n = self.data["attempts"].get(slug, 0) + 1
            self.data["attempts"][slug] = n
            self._save()
            return n

    def swept(self):
        with self.lock:
            self.data["last_sweep"] = now_iso()
            self._save()

    def snapshot(self, slug: str, recipe: dict) -> Path:
        """The recipe exactly as it was before this service wrote to it -- the undo copy."""
        d = self.dir / "snapshots" / _safe(slug)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
        p.write_text(json.dumps(recipe, indent=1, ensure_ascii=False))
        return p

    def latest_snapshot(self, slug: str) -> Path | None:
        d = self.dir / "snapshots" / _safe(slug)
        snaps = sorted(d.glob("*.json")) if d.exists() else []
        return snaps[-1] if snaps else None

    def save_run(self, slug: str, record: dict) -> Path:
        d = self.dir / "runs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{_safe(slug)}.json"
        p.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str))
        return p
