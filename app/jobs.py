"""Estado de los jobs y cola de trabajo, ambos en Redis.

- Cada job es un hash `job:<id>` con su estado (PENDING/RUNNING/AWAITING_APPROVAL/
  DONE/FAILED), la pregunta, el resultado o el error.
- La cola es una lista `jobs:queue`: la API hace RPUSH y los workers BLPOP. Como vive
  en Redis, los trabajos pendientes sobreviven a un reinicio y se pueden repartir
  entre varios procesos worker.
- Todo es asíncrono (`redis.asyncio`): ninguna llamada bloquea el event loop.
"""

import json
import time
import uuid
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

QUEUE_KEY = "jobs:queue"
JOB_PREFIX = "job:"
# Campos que se guardan como JSON dentro del hash.
JSON_FIELDS = {"resultado", "aprobacion_pendiente", "decision_humana"}


class Status:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    DONE = "DONE"
    FAILED = "FAILED"
    TERMINALES = {DONE, FAILED}


class JobStore:
    def __init__(self, redis: Redis, ttl_seconds: int = 7 * 24 * 3600) -> None:
        self.redis = redis
        self.ttl = ttl_seconds

    @staticmethod
    def _key(job_id: str) -> str:
        return f"{JOB_PREFIX}{job_id}"

    @staticmethod
    def _encode(campos: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for clave, valor in campos.items():
            if valor is None:
                out[clave] = ""
            elif clave in JSON_FIELDS:
                out[clave] = json.dumps(valor, ensure_ascii=False)
            else:
                out[clave] = str(valor)
        return out

    @staticmethod
    def _decode(crudo: dict[str, str]) -> dict[str, Any]:
        job: dict[str, Any] = {}
        for clave, valor in crudo.items():
            if valor == "":
                job[clave] = None
            elif clave in JSON_FIELDS:
                job[clave] = json.loads(valor)
            else:
                job[clave] = valor
        return job

    async def create(self, pregunta: str) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        ahora = time.time()
        campos = {
            "id": job_id,
            "status": Status.PENDING,
            "pregunta": pregunta,
            "created_at": ahora,
            "updated_at": ahora,
        }
        await self.redis.hset(self._key(job_id), mapping=self._encode(campos))
        await self.redis.expire(self._key(job_id), self.ttl)
        return await self.get(job_id) or campos

    async def get(self, job_id: str) -> dict[str, Any] | None:
        crudo = await self.redis.hgetall(self._key(job_id))
        return self._decode(crudo) if crudo else None

    async def update(self, job_id: str, **campos: Any) -> None:
        campos["updated_at"] = time.time()
        await self.redis.hset(self._key(job_id), mapping=self._encode(campos))

    async def transition(self, job_id: str, esperado: str, nuevo: str, **campos: Any) -> bool:
        """Cambia el estado solo si sigue siendo `esperado` (transacción optimista
        con WATCH). Devuelve False si otro proceso lo cambió antes."""
        key = self._key(job_id)
        campos.update(status=nuevo, updated_at=time.time())
        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if await pipe.hget(key, "status") != esperado:
                    return False
                pipe.multi()
                pipe.hset(key, mapping=self._encode(campos))
                await pipe.execute()
                return True
            except WatchError:
                return False

    async def enqueue(self, mensaje: dict[str, Any]) -> None:
        await self.redis.rpush(QUEUE_KEY, json.dumps(mensaje))

    async def dequeue(self, timeout: int = 1) -> dict[str, Any] | None:
        """Espera hasta `timeout` segundos un trabajo (BLPOP asíncrono)."""
        item = await self.redis.blpop([QUEUE_KEY], timeout=timeout)
        return json.loads(item[1]) if item else None

    async def queue_length(self) -> int:
        return int(await self.redis.llen(QUEUE_KEY))
