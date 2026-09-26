"""The webhook receiver and the single worker thread behind it.

Mealie's `recipe_created` event is used as a trigger, never as the source of truth:

* a single-URL import sends the new slug, but
* a bulk URL import sends ONE event carrying only a report id, dispatched before scraping
  has even started, and the report never lists slugs.

So every event just schedules a sweep. The worker waits for any bulk report to finish,
waits a settle period so a burst of events becomes one sweep, then processes whatever is
unprocessed. A periodic sweep catches anything whose event was lost.
"""

import json
import logging
import queue
import threading
import time
import urllib.parse

from pydantic import ValidationError

from .models import AppriseEvent
from .pipeline import Pipeline

log = logging.getLogger(__name__)

REPORT_POLL_SECONDS = 10
REPORT_MAX_WAIT = 45 * 60


class Worker:
    def __init__(self, pipeline: Pipeline, settle_seconds: int, sweep_minutes: int):
        self.pipeline = pipeline
        self.settle = settle_seconds
        self.sweep_minutes = sweep_minutes
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.last_results: list[dict] = []

    def trigger(self, reason: str, report_id: str | None = None):
        self.q.put((reason, report_id))

    def start(self):
        threading.Thread(target=self._run, name="worker", daemon=True).start()
        if self.sweep_minutes > 0:
            threading.Thread(target=self._tick, name="ticker", daemon=True).start()

    def _tick(self):
        while True:
            time.sleep(self.sweep_minutes * 60)
            self.trigger("periodic")

    def _run(self):
        while True:
            reason, report = self.q.get()
            reasons, reports = {reason}, {report} - {None}
            # Coalesce everything that arrives during the settle period into one sweep.
            deadline = time.monotonic() + (0 if reason == "periodic" else self.settle)
            while (left := deadline - time.monotonic()) > 0:
                try:
                    r, rep = self.q.get(timeout=left)
                    reasons.add(r)
                    if rep:
                        reports.add(rep)
                except queue.Empty:
                    break
            for rep in reports:
                self._wait_for_report(rep)
            self.busy = True
            try:
                log.info("sweep (%s)", ", ".join(sorted(reasons)))
                results = self.pipeline.sweep()
                self.last_results = [r.model_dump(include={"slug", "status", "reason", "flags"})
                                     for r in results]
                for r in results:
                    log.info("  %s: %s %s", r.slug, r.status, r.reason)
            except Exception:
                log.exception("sweep failed")
            finally:
                self.busy = False

    def _wait_for_report(self, report_id: str):
        """A bulk import's event arrives before its scraping starts; wait for it to end."""
        start = time.monotonic()
        while time.monotonic() - start < REPORT_MAX_WAIT:
            try:
                status = self.pipeline.mealie.report(report_id).get("status")
            except Exception as e:
                log.warning("bulk report %s unreadable: %s", report_id, e)
                return
            if status != "in-progress":
                log.info("bulk report %s finished: %s", report_id, status)
                return
            time.sleep(REPORT_POLL_SECONDS)
        log.warning("bulk report %s still running after %ds; sweeping anyway",
                    report_id, REPORT_MAX_WAIT)


def parse_document_data(raw: str) -> dict:
    """Mealie URL-encodes document_data into the Apprise URL and Apprise never decodes it,
    so it arrives as `{"documentType":+"recipe_bulk_report",+...}` -- '+' for every space."""
    for candidate in (raw or "{}", urllib.parse.unquote_plus(raw or "{}")):
        try:
            doc = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            return doc
    return {}


def handle_event(worker: Worker, body: bytes) -> str:
    """Interpret one notifier POST. Returns a short description for the log."""
    try:
        ev = AppriseEvent.model_validate_json(body or b"{}")
    except ValidationError as e:
        return f"ignored unparseable payload: {e.error_count()} error(s)"
    if ev.event_type != "recipe_created":
        return f"ignored {ev.event_type or 'untyped'} event"
    doc = parse_document_data(ev.document_data)
    report = doc.get("report_id") or doc.get("reportId")
    slug = doc.get("recipe_slug") or doc.get("recipeSlug")
    worker.trigger("event", report_id=str(report) if report else None)
    return f"recipe_created ({'bulk report ' + str(report) if report else slug})"


def serve(pipeline: Pipeline, port: int, settle_seconds: int, sweep_minutes: int):
    import uvicorn

    from .web import create_app

    # Fix the cut-off NOW. Set lazily by the first sweep, it landed after the settle period,
    # and a recipe imported during that window fell before it and was never processed.
    since = pipeline.state.since(pipeline.cfg.process_since)
    log.info("processing scraped recipes created since %s", since.isoformat())
    worker = Worker(pipeline, settle_seconds, sweep_minutes)
    worker.start()
    worker.trigger("startup")          # catch anything imported while we were down
    log.info("listening on :%d (settle %ds, sweep every %d min)", port, settle_seconds,
             sweep_minutes)
    # uvicorn handles SIGTERM itself, so a container stop is immediate.
    uvicorn.run(create_app(pipeline, worker), host="0.0.0.0", port=port,
                log_level="warning", access_log=False)
