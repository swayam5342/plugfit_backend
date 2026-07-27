from celery import Celery
from plugfit.app.config import settings

REDIS_URL = settings.REDIS_URL

celery_app = Celery(
    "plugfit",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["plugfit.app.job.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=settings.PIPELINE_WORKERS,
    result_expires=60 * 60 * 24,
    task_default_queue="pipeline",
    task_routes={
        "plugfit.app.job.tasks.run_pipeline": {"queue": "pipeline"},
        "plugfit.app.job.tasks.run_eval_only": {"queue": "pipeline"},
    },
    task_max_retries=settings.PIPELINE_MAX_RETRY,
    task_default_retry_delay=settings.PIPELINE_TIMEOUT_SECONDS,
    timezone="UTC",
    enable_utc=True,
)
