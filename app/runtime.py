"""Construye los componentes compartidos (Redis, checkpointer, grafo, observabilidad)
tanto para la API como para un worker independiente."""

import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from redis.asyncio import Redis

from app.config import Settings
from app.graph import build_graph, create_llm
from app.jobs import JobStore
from app.observability import init_observability

logger = logging.getLogger("runtime")


@dataclass
class Runtime:
    redis: Redis
    store: JobStore
    graph: CompiledStateGraph
    observability: dict[str, Any]


async def build_runtime(
    stack: AsyncExitStack,
    settings: Settings,
    *,
    redis_client: Redis | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    llm: BaseChatModel | None = None,
) -> Runtime:
    """Los parámetros opcionales permiten inyectar piezas falsas en los tests."""
    redis = redis_client or Redis.from_url(settings.redis_url, decode_responses=True)
    stack.push_async_callback(redis.aclose)
    try:
        await redis.ping()  # falla rápido, con un mensaje claro, si Redis no está
    except Exception as error:
        raise RuntimeError(
            f"No se pudo conectar a Redis en {settings.redis_url} ({type(error).__name__}). "
            "Levantalo con `docker compose up -d redis` o revisá REDIS_URL."
        ) from error

    observabilidad = init_observability(settings)

    if checkpointer is None:
        if settings.checkpointer == "redis":
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver

            checkpointer = await stack.enter_async_context(AsyncRedisSaver.from_conn_string(settings.redis_url))
            try:
                await checkpointer.asetup()
            except Exception as error:
                raise RuntimeError(
                    "No se pudo inicializar el checkpointer de Redis. AsyncRedisSaver necesita Redis "
                    "con los módulos RedisJSON y RediSearch (imagen `redis/redis-stack-server`, o Redis "
                    f"Cloud). Detalle: {type(error).__name__}: {error}"
                ) from error
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            logger.warning("CHECKPOINTER=%s: el estado del grafo NO es persistente.", settings.checkpointer)
            checkpointer = InMemorySaver()

    if llm is None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Falta OPENAI_API_KEY (definila en el archivo .env).")
        llm = create_llm(settings.openai_model)

    graph = build_graph(llm, checkpointer)
    return Runtime(redis=redis, store=JobStore(redis, settings.job_ttl_seconds), graph=graph, observability=observabilidad)
