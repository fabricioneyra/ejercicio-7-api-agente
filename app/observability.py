"""Capa de observabilidad: envía una traza de cada ejecución a Phoenix o LangSmith.

- **Phoenix (Arize)**: `register()` configura un exportador OpenTelemetry hacia el
  colector de Phoenix y `LangChainInstrumentor` instrumenta LangChain/LangGraph.
  Cada llamada a un LLM, herramienta, nodo y agente aparece como un *span* con sus
  tokens de entrada/salida, y Phoenix calcula el costo a partir de esos tokens.
- **LangSmith**: alcanza con variables de entorno; LangChain traza solo.

No hace falta decorar a mano cada función: la instrumentación cubre todas las
llamadas hechas a través de LangChain. Lo que sí hace la app es etiquetar cada
corrida con el `job_id` (ver `worker.py`) para encontrar su traza en el dashboard.
"""

import logging
import os
from typing import Any

from app.config import Settings

logger = logging.getLogger("observability")


def init_observability(settings: Settings) -> dict[str, Any]:
    """Inicializa el backend elegido. Nunca tira abajo la API: si falla, lo informa
    en el log y en `/health`."""
    modo = settings.observability
    info: dict[str, Any] = {"mode": modo, "active": False, "provider": None}

    try:
        if modo == "phoenix":
            from openinference.instrumentation.langchain import LangChainInstrumentor
            from phoenix.otel import register

            provider = register(
                project_name=settings.phoenix_project,
                endpoint=f"{settings.phoenix_endpoint.rstrip('/')}/v1/traces",
                protocol="http/protobuf",
                batch=True,
                verbose=False,
                # Importante: NO registrarlo como proveedor global. Las versiones nuevas de
                # FastAPI/Starlette emiten un span por cada request HTTP (POST /tasks,
                # GET /tasks/..., que se consulta cada segundo) en cuanto hay un proveedor
                # global, y eso llenaría el dashboard de ruido y arruinaría los percentiles
                # de latencia. Solo queremos trazas del agente: el instrumentador de
                # LangChain recibe el proveedor de forma explícita.
                set_global_tracer_provider=False,
            )
            LangChainInstrumentor().instrument(tracer_provider=provider)
            info.update(active=True, provider=provider, project=settings.phoenix_project,
                        endpoint=settings.phoenix_endpoint)
        elif modo == "langsmith":
            if not os.environ.get("LANGSMITH_API_KEY"):
                raise RuntimeError("Falta LANGSMITH_API_KEY en el entorno.")
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
            info.update(active=True, project=os.environ["LANGSMITH_PROJECT"])
        elif modo != "none":
            raise RuntimeError(f"OBSERVABILITY='{modo}' no es válido (phoenix | langsmith | none).")
    except Exception as error:
        logger.warning("Observabilidad desactivada: %s: %s", type(error).__name__, error)
        info["error"] = f"{type(error).__name__}: {error}"
    return info


def flush_observability(info: dict[str, Any]) -> None:
    """Envía las trazas pendientes al apagar (el exportador trabaja en lotes)."""
    provider = info.get("provider")
    if provider is not None:
        try:
            provider.force_flush()
        except Exception as error:  # pragma: no cover
            logger.warning("No se pudieron enviar las últimas trazas: %s", error)


def public_info(info: dict[str, Any]) -> dict[str, Any]:
    """Versión serializable (sin el objeto provider) para `/health`."""
    return {k: v for k, v in info.items() if k != "provider"}
