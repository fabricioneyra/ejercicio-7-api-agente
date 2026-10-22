"""Configuración de la aplicación, leída de variables de entorno (.env)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _bool(valor: str | None, por_defecto: bool) -> bool:
    if valor is None:
        return por_defecto
    return valor.strip().lower() in {"1", "true", "yes", "si", "sí"}


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://localhost:6379/0"
    openai_model: str = "gpt-4o-mini"
    # phoenix | langsmith | none
    observability: str = "none"
    phoenix_endpoint: str = "http://localhost:6006"
    phoenix_project: str = "agente-multiagente-api"
    langsmith_project: str = "agente-multiagente-api"
    worker_concurrency: int = 5
    run_worker_in_api: bool = True
    job_timeout_seconds: int = 180
    job_ttl_seconds: int = 7 * 24 * 3600
    # redis (por defecto) | memory (solo desarrollo/tests: se pierde al reiniciar)
    checkpointer: str = "redis"


def get_settings() -> Settings:
    load_dotenv()
    e = os.environ.get
    return Settings(
        redis_url=e("REDIS_URL", Settings.redis_url),
        openai_model=e("OPENAI_MODEL", Settings.openai_model),
        observability=e("OBSERVABILITY", Settings.observability).strip().lower(),
        phoenix_endpoint=e("PHOENIX_COLLECTOR_ENDPOINT", Settings.phoenix_endpoint),
        phoenix_project=e("PHOENIX_PROJECT_NAME", Settings.phoenix_project),
        langsmith_project=e("LANGSMITH_PROJECT", Settings.langsmith_project),
        worker_concurrency=int(e("WORKER_CONCURRENCY", Settings.worker_concurrency)),
        run_worker_in_api=_bool(e("RUN_WORKER_IN_API"), True),
        job_timeout_seconds=int(e("JOB_TIMEOUT_SECONDS", Settings.job_timeout_seconds)),
        checkpointer=e("CHECKPOINTER", Settings.checkpointer).strip().lower(),
    )
