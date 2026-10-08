from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy.exc import OperationalError

from ..db import SessionLocal
from ..models import Job


class JobManager:
    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}
        self._write_lock = asyncio.Lock()

    async def create(self, kind: str, worker):
        job_id = uuid.uuid4().hex[:16]
        async with self._write_lock:
            async with SessionLocal() as session:
                session.add(Job(id=job_id, kind=kind, status="queued", message="В очереди"))
                await self._commit_with_retry(session)
        task = asyncio.create_task(self._run(job_id, worker))
        self.tasks[job_id] = task
        return job_id

    async def _run(self, job_id: str, worker):
        await self.update(job_id, status="running", message="Запущено")
        try:
            await worker(job_id, self)
        except Exception as exc:  # noqa: BLE001
            try:
                await self.update(job_id, status="failed", message=str(exc), errors=[str(exc)])
            except Exception:
                pass
        finally:
            self.tasks.pop(job_id, None)

    async def update(
        self,
        job_id: str,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        status: str | None = None,
        errors: list[str] | None = None,
    ):
        # SQLite has one writer. Serialize tiny Job updates and retry transient locks.
        async with self._write_lock:
            async with SessionLocal() as session:
                job = await session.get(Job, job_id)
                if not job:
                    return
                if current is not None:
                    job.current = current
                if total is not None:
                    job.total = total
                if message is not None:
                    job.message = message
                if status is not None:
                    job.status = status
                if errors is not None:
                    job.errors_json = json.dumps(errors, ensure_ascii=False)
                await self._commit_with_retry(session)

    async def get(self, job_id: str) -> Job | None:
        async with SessionLocal() as session:
            return await session.get(Job, job_id)

    @staticmethod
    async def _commit_with_retry(session, attempts: int = 6):
        for attempt in range(attempts):
            try:
                await session.commit()
                return
            except OperationalError as exc:
                await session.rollback()
                if "locked" not in str(exc).lower() or attempt == attempts - 1:
                    raise
                await asyncio.sleep(0.15 * (attempt + 1))
