"""
Per-user job queue.

* Har user ke jobs ek ke baad ek chalte hain (order maintain rehta hai)
* Global semaphore: ek saath max MAX_CONCURRENT_JOBS ffmpeg jobs (server RAM bachane ke liye)
* /stop: chalta hua job cancel + queue clear
"""

import asyncio
from collections import deque

from config import MAX_CONCURRENT_JOBS
from logger import LOGGER

logger = LOGGER(__name__)

_SEM = None


def _sem():
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    return _SEM


class Job:
    def __init__(self, run, weight=1, kind="audio", on_dropped=None):
        self.run = run              # async callable
        self.weight = weight        # kitni audio count hoti hai (limit reservation)
        self.kind = kind            # audio | batch | merge
        self.on_dropped = on_dropped  # async callable: queue se hatane par (/stop)


class _UserJobs:
    def __init__(self):
        self.queue = deque()
        self.runner = None
        self.current = None       # asyncio.Task
        self.current_job = None


JOBS = {}


def _get(uid):
    if uid not in JOBS:
        JOBS[uid] = _UserJobs()
    return JOBS[uid]


def pending_weight(uid):
    """Queue + running jobs ki total audio count (daily-limit reservation ke liye)."""
    uj = JOBS.get(uid)
    if not uj:
        return 0
    n = sum(j.weight for j in uj.queue)
    if uj.current_job is not None:
        n += uj.current_job.weight
    return n


def queue_position(uid):
    """Naya job add hone ke baad uski position (1 = turant chalega)."""
    uj = JOBS.get(uid)
    if not uj:
        return 0
    return len(uj.queue) + (1 if uj.current_job is not None else 0)


def is_busy(uid):
    uj = JOBS.get(uid)
    return bool(uj and (uj.queue or uj.current_job is not None))


async def enqueue(uid, job):
    uj = _get(uid)
    uj.queue.append(job)
    if uj.runner is None or uj.runner.done():
        uj.runner = asyncio.create_task(_runner(uid))


async def _runner(uid):
    uj = _get(uid)
    while uj.queue:
        job = uj.queue.popleft()
        uj.current_job = job

        async def _wrapped(j=job):
            async with _sem():
                await j.run()

        uj.current = asyncio.create_task(_wrapped())
        await asyncio.wait({uj.current})
        t = uj.current
        if not t.cancelled() and t.exception() is not None:
            logger.error(f"Job crashed for {uid}: {t.exception()!r}")
        uj.current = None
        uj.current_job = None


async def stop_all(uid):
    """
    /stop: queue clear + running job cancel.
    Returns (cleared_jobs_weight, was_running)
    """
    uj = JOBS.get(uid)
    if not uj:
        return 0, False
    dropped = list(uj.queue)
    uj.queue.clear()
    cleared = sum(j.weight for j in dropped)
    for j in dropped:
        if j.on_dropped:
            try:
                await j.on_dropped()
            except Exception:
                pass
    was_running = uj.current is not None and not uj.current.done()
    if was_running:
        uj.current.cancel()
        try:
            await asyncio.wait({uj.current}, timeout=15)
        except Exception:
            pass
    return cleared, was_running
