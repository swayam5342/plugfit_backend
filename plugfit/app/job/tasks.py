import logging
from datetime import datetime, timezone

from sqlalchemy import select

from .celery import celery_app
from plugfit.app.db.db_sync import sync_db_session
from .cleaner import clean_manifest, heal_manifest
from .scorer import heuristic_score, gemini_score
from plugfit.app.models.models import Job, JobStatus, Server, ServerStatus
from plugfit.app.config import settings

log = logging.getLogger("plugfit.tasks")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(job_id: str, stage: str, message: str) -> None:
    with sync_db_session() as db:
        job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if not job:
            return
        entry = {"ts": _ts(), "stage": stage, "message": message}
        job.logs = [*job.logs, entry]
        job.stage = stage
    log.info("[job:%s] [%s] %s", job_id[:8], stage, message)


def _set_job(job_id: str, status: JobStatus, error: str | None = None) -> None:
    with sync_db_session() as db:
        job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if job:
            job.status = status
            if error:
                job.error = error[:2000]


def _set_server_status(server_id: str, status: ServerStatus) -> None:
    with sync_db_session() as db:
        s = db.execute(
            select(Server).where(Server.id == server_id)
        ).scalar_one_or_none()
        if s:
            s.status = status


@celery_app.task(
    name="plugfit.platform.jobs.tasks.run_pipeline",
    bind=True,
    max_retries=settings.PIPELINE_MAX_RETRY,
    default_retry_delay=settings.PIPELINE_TIMEOUT_SECONDS,
    acks_late=True,
)
def run_pipeline(self, server_id: str, job_id: str) -> dict:
    log.info("Pipeline started: server=%s job=%s", server_id[:8], job_id[:8])
    _set_job(job_id, JobStatus.RUNNING)

    try:
        _append_log(job_id, "loading", "Loading server from database")
        with sync_db_session() as db:
            server = db.execute(
                select(Server).where(Server.id == server_id)
            ).scalar_one_or_none()
            if not server:
                raise RuntimeError(f"Server {server_id} not found in database")

            raw_manifest = server.raw_manifest
            server_name = server.name

        if not raw_manifest:
            raise RuntimeError("Server has no raw_manifest — was ingest completed?")

        tool_count_before = len(raw_manifest.get("tools", []))
        _append_log(
            job_id, "loading", f"Loaded {tool_count_before} tools from '{server_name}'"
        )
        _append_log(job_id, "scoring", "Scoring raw manifest (before cleaning)")
        score_before = heuristic_score(raw_manifest)
        _append_log(job_id, "scoring", f"Before score: {score_before}/100 (heuristic)")

        # ── Stage 3: Clean ────────────────────────────────────────────────────
        _append_log(job_id, "cleaning", "Running Gemini cleaning engine")
        _append_log(job_id, "cleaning", "Step 1/3: normalising tool names")

        cleaned_manifest = clean_manifest(raw_manifest)

        meta = cleaned_manifest.get("_cleaning_meta", {})
        tool_count_after = cleaned_manifest.get("tool_count", 0)
        _append_log(job_id, "cleaning", f"Step 2/3: rewrote descriptions via Gemini")
        if meta.get("dropped"):
            _append_log(
                job_id, "cleaning", f"Step 3/3: dropped dead tools: {meta['dropped']}"
            )
        if meta.get("merged"):
            _append_log(
                job_id, "cleaning", f"Step 3/3: merged duplicates: {meta['merged']}"
            )

        _append_log(
            job_id,
            "cleaning",
            f"Cleaning done: {tool_count_before} → {tool_count_after} tools",
        )

        # ── Stage 4: Score after ──────────────────────────────────────────────
        _append_log(job_id, "evaluating", "Scoring cleaned manifest with Gemini")
        score_after, feedback = gemini_score(cleaned_manifest)

        _append_log(
            job_id,
            "healing",
            "Running self-healing pass for low-quality tool descriptions",
        )
        healed_manifest, healed_score, healed_feedback = heal_manifest(
            cleaned_manifest,
            feedback=feedback,
            initial_score=score_after,
            max_attempts=3,
        )

        _append_log(
            job_id,
            "evaluating",
            f"After score: {healed_score}/100  (Δ {healed_score - score_before:+.1f})",
        )
        if healed_manifest.get("_healing_meta", {}).get("improved"):
            _append_log(
                job_id,
                "healing",
                f"Self-healing improved the manifest by {healed_score - score_after:+.1f} points",
            )
        else:
            _append_log(job_id, "healing", "Self-healing did not materially improve the manifest")

        bad = [
            f["name"]
            for f in healed_feedback
            if (f.get("clarity", 10) < 5 or f.get("selectability", 10) < 5)
        ]
        if bad:
            _append_log(job_id, "evaluating", f"Tools still needing attention: {bad}")
        _append_log(job_id, "saving", "Persisting results to database")
        with sync_db_session() as db:
            server = db.execute(
                select(Server).where(Server.id == server_id)
            ).scalar_one_or_none()
            if server:
                server.cleaned_manifest = healed_manifest
                server.score_before = score_before
                server.score_after = healed_score
                server.tool_count_after = tool_count_after
                server.status = ServerStatus.READY

        _set_job(job_id, JobStatus.DONE)
        _append_log(
            job_id,
            "done",
            f"Pipeline complete ✓  Score: {score_before} → {healed_score}",
        )

        log.info(
            "Pipeline done: server=%s score=%s→%s tools=%d→%d",
            server_id[:8],
            score_before,
            healed_score,
            tool_count_before,
            tool_count_after,
        )

        return {
            "server_id": server_id,
            "score_before": score_before,
            "score_after": healed_score,
            "tool_count_before": tool_count_before,
            "tool_count_after": tool_count_after,
        }

    except Exception as exc:
        log.exception("Pipeline failed: server=%s job=%s", server_id[:8], job_id[:8])
        err_msg = str(exc)

        _set_job(job_id, JobStatus.FAILED, error=err_msg)
        _set_server_status(server_id, ServerStatus.ERROR)
        _append_log(job_id, "failed", f"Pipeline failed: {err_msg}")
        transient = any(
            k in err_msg.lower()
            for k in ("rate limit", "quota", "timeout", "connection", "503", "429")
        )
        if transient:
            raise self.retry(exc=exc)
        raise
