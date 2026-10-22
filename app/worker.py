"""Worker: toma jobs de la cola de Redis, ejecuta el grafo y actualiza el estado.

Estados: PENDING -> RUNNING -> (AWAITING_APPROVAL -> PENDING -> RUNNING ->) DONE | FAILED

Cualquier excepción se captura y el job pasa a FAILED con el error (si no, el
cliente quedaría esperando para siempre). También hay un timeout por job.

Por defecto corre dentro del proceso de la API (tareas asyncio). Para escalar se
puede lanzar aparte con `python -m app.worker`; ambos leen la misma cola en Redis.
"""

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.config import Settings
from app.graph import RECURSION_LIMIT, estado_inicial
from app.jobs import JobStore, Status

logger = logging.getLogger("worker")


class Worker:
    def __init__(self, graph: CompiledStateGraph, store: JobStore, settings: Settings) -> None:
        self.graph = graph
        self.store = store
        self.settings = settings
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    # --- ciclo de vida --------------------------------------------------------
    def start(self, concurrency: int | None = None) -> None:
        n = concurrency or self.settings.worker_concurrency
        self._stop.clear()
        self._tasks = [asyncio.create_task(self._consumir(i), name=f"worker-{i}") for i in range(n)]
        logger.info("Worker iniciado con %d consumidores.", n)

    async def stop(self, gracia: float = 5.0) -> None:
        """Apagado ordenado.

        Primero se avisa a los consumidores y se espera. Los que están ociosos
        terminan solos en <= 1 s (vence su espera de la cola); los que están
        ejecutando un job tienen `gracia` segundos para terminar. Recién después se
        cancela lo que quede. Cancelar un consumidor en medio de la lectura de la
        cola (BLPOP) podría perder un mensaje: el servidor se lo entregaría a una
        conexión ya cerrada.
        """
        self._stop.set()
        if self._tasks:
            _, pendientes = await asyncio.wait(self._tasks, timeout=gracia)
            for tarea in pendientes:
                tarea.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _consumir(self, indice: int) -> None:
        while not self._stop.is_set():
            try:
                mensaje = await self.store.dequeue(timeout=1)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # Redis caído, etc.: reintentar sin matar al consumidor
                logger.error("worker-%d no pudo leer la cola: %s", indice, error)
                await asyncio.sleep(2)
                continue
            if mensaje is not None:
                await self.procesar(mensaje)

    # --- procesamiento de un job ----------------------------------------------
    async def procesar(self, mensaje: dict[str, Any]) -> None:
        job_id = mensaje["job_id"]
        try:
            job = await self.store.get(job_id)
            if job is None:
                logger.warning("Job %s no existe (¿venció su TTL?).", job_id)
                return
            await self.store.update(job_id, status=Status.RUNNING, error=None, aprobacion_pendiente=None)

            if mensaje.get("action") == "resume":
                entrada: Any = Command(resume=mensaje["decision"])
            else:
                entrada = estado_inicial(job["pregunta"])

            config = {
                "configurable": {"thread_id": job_id},  # el checkpoint de Redis se indexa por esto
                "recursion_limit": RECURSION_LIMIT,
                # Etiquetas para encontrar la traza de este job en Phoenix/LangSmith.
                "run_name": f"job-{job_id[:8]}",
                "tags": ["api", "multiagente"],
                "metadata": {"job_id": job_id, "session_id": job_id},
            }

            async with asyncio.timeout(self.settings.job_timeout_seconds):
                async for update in self.graph.astream(entrada, config, stream_mode="updates"):
                    nodos = [n for n in update if not n.startswith("__")]
                    if nodos:
                        await self.store.update(job_id, ultimo_nodo=nodos[-1])

            await self._cerrar(job_id, config)

        except asyncio.CancelledError:
            await self._marcar_fallido(job_id, "Interrumpido: la aplicación se estaba apagando.")
            raise
        except TimeoutError:
            await self._marcar_fallido(job_id, f"Timeout: superó {self.settings.job_timeout_seconds} s.")
        except Exception as error:
            logger.exception("Job %s falló", job_id)
            await self._marcar_fallido(job_id, f"{type(error).__name__}: {error}")

    async def _cerrar(self, job_id: str, config: dict[str, Any]) -> None:
        """Decide el estado final según cómo terminó el grafo."""
        snapshot = await self.graph.aget_state(config)
        interrupciones = [i for tarea in snapshot.tasks for i in tarea.interrupts]
        if interrupciones:
            # El grafo quedó pausado en el nodo de aprobación humana.
            await self.store.update(
                job_id, status=Status.AWAITING_APPROVAL, aprobacion_pendiente=interrupciones[0].value
            )
            return

        valores = snapshot.values
        respuesta = next(
            (str(m.content) for m in reversed(valores.get("messages", []))
             if isinstance(m, AIMessage) and m.name == "sintesis"),
            "",
        )
        resultado = {
            "respuesta": respuesta,
            "flujo": [d["siguiente"] for d in valores.get("decisiones", [])],
            "aprobacion": valores.get("aprobacion"),
            "aportes": [
                {"agente": c["agente"], "herramientas": c["herramientas"], "resultado": c["resultado"][:1500]}
                for c in valores.get("contribuciones", [])
            ],
        }
        await self.store.update(job_id, status=Status.DONE, resultado=resultado, error=None)

    async def _marcar_fallido(self, job_id: str, error: str) -> None:
        try:
            await self.store.update(job_id, status=Status.FAILED, error=error)
        except Exception:  # si Redis también falló no hay más que hacer
            logger.exception("No se pudo marcar el job %s como FAILED", job_id)


async def run_standalone() -> None:
    """Worker como proceso separado: `python -m app.worker`."""
    from contextlib import AsyncExitStack

    from app.config import get_settings
    from app.runtime import build_runtime

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = get_settings()
    async with AsyncExitStack() as stack:
        rt = await build_runtime(stack, settings)
        worker = Worker(rt.graph, rt.store, settings)
        worker.start()
        try:
            await asyncio.Event().wait()
        finally:
            await worker.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run_standalone())
    except KeyboardInterrupt:
        pass
