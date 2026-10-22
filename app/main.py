"""API REST (FastAPI) del sistema multi-agente.

    POST /tasks                 -> encola un trabajo y devuelve un job_id (no bloquea)
    GET  /tasks/{id}            -> estado del trabajo (leído de Redis)
    POST /tasks/{id}/approve    -> aprueba/rechaza una acción crítica (ver hitl.py)
    GET  /health                -> estado de Redis, cola y observabilidad

Ningún endpoint ejecuta el grafo: solo escriben en Redis. El trabajo pesado lo hace
el worker (app/worker.py), así que la API responde en milisegundos aunque haya
muchos agentes corriendo.
"""

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.hitl import router as hitl_router
from app.jobs import Status
from app.observability import flush_observability, public_info
from app.runtime import build_runtime
from app.worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class TaskRequest(BaseModel):
    pregunta: str = Field(min_length=3, max_length=2000, examples=["¿Cuánto cuesta tener la Tarjeta Plus durante 3 años?"])


def create_app(
    settings: Settings | None = None,
    *,
    redis_client: Redis | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    llm: BaseChatModel | None = None,
) -> FastAPI:
    """Fábrica de la aplicación. Los argumentos opcionales sirven para los tests."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            rt = await build_runtime(stack, settings, redis_client=redis_client, checkpointer=checkpointer, llm=llm)
            app.state.store = rt.store
            app.state.redis = rt.redis
            app.state.observability = rt.observability
            app.state.settings = settings
            worker = Worker(rt.graph, rt.store, settings)
            app.state.worker = worker
            if settings.run_worker_in_api:
                worker.start()
            try:
                yield
            finally:
                await worker.stop()
                flush_observability(rt.observability)

    app = FastAPI(
        title="API del orquestador multi-agente",
        description="Encola tareas para un sistema multi-agente (LangGraph), con estado en Redis, "
        "trazas en Phoenix/LangSmith y aprobación humana para acciones críticas.",
        lifespan=lifespan,
    )
    app.include_router(hitl_router)

    @app.exception_handler(RedisError)
    async def redis_error_handler(_: Request, error: RedisError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": f"Redis no disponible: {type(error).__name__}"})

    @app.post("/tasks", status_code=202)
    async def create_task(body: TaskRequest, request: Request) -> dict[str, Any]:
        """Registra el trabajo (PENDING), lo encola y devuelve el job_id al instante."""
        store = request.app.state.store
        job = await store.create(body.pregunta)
        await store.enqueue({"job_id": job["id"], "action": "start"})
        return {"job_id": job["id"], "status": Status.PENDING, "status_url": f"/tasks/{job['id']}"}

    @app.get("/tasks/{job_id}")
    async def get_task(job_id: str, request: Request) -> dict[str, Any]:
        """Estado del trabajo. Cuando es DONE trae el resultado; si es FAILED, el error;
        si es AWAITING_APPROVAL, la acción que espera una decisión humana."""
        job = await request.app.state.store.get(job_id)
        if job is None:
            raise HTTPException(404, f"No existe el job {job_id}.")
        if job["status"] in Status.TERMINALES:
            job["duracion_segundos"] = round(float(job["updated_at"]) - float(job["created_at"]), 2)
        return job

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        await request.app.state.redis.ping()
        return {
            "status": "ok",
            "cola_pendiente": await request.app.state.store.queue_length(),
            "worker_en_la_api": request.app.state.settings.run_worker_in_api,
            "observabilidad": public_info(request.app.state.observability),
        }

    return app


app = create_app()
