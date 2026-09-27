"""Worker process: python -m app.worker

One loop; one second between idle polls. Safe to run more than one copy (FOR UPDATE SKIP LOCKED).
"""
import asyncio
import os
import pathlib
import signal
from datetime import datetime, timezone

from app.bootstrap import build_ctx, build_engine_and_sessions, register_public_key_and_load_all
from app.logs import log
from app.services import worker_jobs as worker_svc
from app.settings import CommonSettings

HEARTBEAT = pathlib.Path(os.environ.get("WORKER_HEARTBEAT_PATH", "run/worker-heartbeat"))
stop_requested = False


def request_stop(*_):
    global stop_requested
    stop_requested = True


async def run_worker(session_factory, ctx, poll_seconds=1.0, max_loops=None):
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    loops = 0
    while not stop_requested:
        now = datetime.now(timezone.utc)
        jobs_done = 0
        transfers_done = 0
        # 1. due timers (one job per transaction)
        while await worker_svc.process_one_job(session_factory, ctx, now):
            jobs_done = jobs_done + 1
        # 2. pending transfers
        while await worker_svc.relay_one_outbox(session_factory, ctx, now):
            transfers_done = transfers_done + 1
        did_work = jobs_done + transfers_done > 0
        if did_work:
            log("worker", jobs=jobs_done, transfers=transfers_done)
        # 3. automatic checkpoint every 100 events
        await worker_svc.maybe_auto_checkpoint(session_factory, ctx, now)
        # 4. heartbeat for the container health check
        HEARTBEAT.write_text(now.isoformat())
        loops = loops + 1
        if max_loops is not None and loops >= max_loops:
            return
        if not did_work:
            await asyncio.sleep(poll_seconds)


async def main():
    settings = CommonSettings()
    ctx = build_ctx(settings)
    engine, sessions = build_engine_and_sessions(settings)
    await register_public_key_and_load_all(sessions, ctx)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        await run_worker(sessions, ctx)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
