from .config import REDIS_URL, RQ_JOB_TIMEOUT, RQ_QUEUE_NAME


def _connection():
    from redis import Redis

    return Redis.from_url(REDIS_URL)


def enqueue_async_task(task_id: int, task_key: str, attempt: int) -> str:
    """Place a database-backed task onto RQ and return its unique job id."""

    from rq import Queue

    job_id = f"{task_key}-attempt-{attempt}"
    queue = Queue(RQ_QUEUE_NAME, connection=_connection())
    queue.enqueue(
        "app.tasks.execute_async_task",
        task_id,
        attempt,
        job_id=job_id,
        job_timeout=RQ_JOB_TIMEOUT,
        result_ttl=86400,
        failure_ttl=604800,
    )
    return job_id


def cancel_queued_job(job_id: str | None) -> None:
    if not job_id:
        return
    from rq.job import Job

    try:
        Job.fetch(job_id, connection=_connection()).cancel()
    except Exception:
        # The database state remains authoritative. A worker that receives a
        # stale queued job checks that state before starting any test.
        return
