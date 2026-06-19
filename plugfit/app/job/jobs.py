import asyncio
import logging

log = logging.getLogger("plugfit.pipeline")


async def run_pipeline_async(server_id: str, job_id: str) -> str:
    try:
        from .tasks import run_pipeline

        result = run_pipeline.delay(server_id, job_id)  # type:ignore
        log.info("Enqueued pipeline task %s for server %s", result.id, server_id[:8])
        return "celery"
    except Exception as e:
        log.warning("Celery unavailable (%s) — running pipeline inline", e)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run_inline, server_id, job_id)
        return "inline"


def _run_inline(server_id: str, job_id: str) -> None:
    """Fallback: run the task synchronously in a thread pool executor."""
    from .tasks import run_pipeline

    try:
        run_pipeline(server_id=server_id, job_id=job_id)  # type:ignore
    except Exception as e:
        log.error("Inline pipeline failed: %s", e)


run_pipeline_sync = run_pipeline_async
