"""Piezas falsas para probar la API sin OpenAI ni Docker.

- `PolicyLLM`: un "LLM" determinista que decide según lo que ve en los mensajes
  (así soporta varias ejecuciones concurrentes) y puede simular lentitud o fallos.
- Redis: `fakeredis`, o un Redis real si se define REDIS_TEST_URL.
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from redis.asyncio import Redis

from app.agents.executor_agent import EFECTOS_SECUNDARIOS
from app.config import Settings
from app.main import create_app
from app.supervisor import Decision


class PolicyLLM(BaseChatModel):
    delay: float = 0.0
    fail_supervisor: bool = False

    @property
    def _llm_type(self) -> str:
        return "policy-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    # --- supervisor (salida estructurada) ---
    def with_structured_output(self, schema, **kwargs):
        async def decidir(mensajes):
            await asyncio.sleep(self.delay)
            if self.fail_supervisor:
                raise RuntimeError("boom: falló el proveedor del LLM")
            return self._decision(str(mensajes[-1].content))

        return RunnableLambda(decidir)

    @staticmethod
    def _decision(texto: str) -> Decision:
        pregunta, _, equipo = texto.partition("APORTES DEL EQUIPO HASTA AHORA:")
        accion = "enviá" in pregunta.lower()
        hay = lambda a: f"{a} (" in equipo  # noqa: E731
        ok = dict(datos_con_fuente=True, calculos_hechos_por_analista=True, responde_toda_la_consulta=True)
        if not hay("investigador"):
            return Decision(razon="faltan datos", siguiente="investigador", instruccion="Buscá reseñas de la Tarjeta Plus",
                            datos_con_fuente=False, calculos_hechos_por_analista=False, responde_toda_la_consulta=False)
        if not hay("analista"):
            return Decision(razon="faltan cálculos", siguiente="analista", instruccion="Calculá el promedio",
                            datos_con_fuente=True, calculos_hechos_por_analista=False, responde_toda_la_consulta=False)
        if accion and not hay("ejecutor") and "RECHAZADA" not in equipo:
            return Decision(razon="hay que enviar el reporte", siguiente="ejecutor",
                            instruccion="Enviá el reporte a ana@example.com con el promedio calculado", **ok)
        return Decision(razon="completo", siguiente="FINISH", **ok)

    # --- agentes y síntesis ---
    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        sistema = str(messages[0].content)
        hubo_tool = any(isinstance(m, ToolMessage) for m in messages)
        llamadas = {
            "INVESTIGACIÓN": ("buscar_conocimiento", {"consulta": "Tarjeta Plus reseñas"}),
            "AGENTE DE ANÁLISIS": ("estadisticas_basicas", {"valores": [5, 4, 2, 5, 3]}),
            "EJECUTOR": ("enviar_reporte_por_email",
                         {"destinatario": "ana@example.com", "asunto": "Reporte", "cuerpo": "Promedio 3.8"}),
        }
        for clave, (nombre, args) in llamadas.items():
            if clave in sistema:
                if not hubo_tool:
                    msg = AIMessage(content="", tool_calls=[{"name": nombre, "args": args, "id": f"c{uuid.uuid4().hex[:8]}"}])
                else:
                    ultimo = [m for m in messages if isinstance(m, ToolMessage)][-1].content
                    msg = AIMessage(content=f"Resultado de {nombre}: {ultimo}")
                return ChatResult(generations=[ChatGeneration(message=msg)])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Respuesta final del equipo."))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        await asyncio.sleep(self.delay)
        return self._generate(messages, stop, run_manager, **kwargs)


@pytest.fixture
def redis_factory():
    """Devuelve una función que crea clientes Redis sobre el MISMO almacenamiento."""
    url = os.environ.get("REDIS_TEST_URL")
    server = fakeredis.FakeServer()

    def crear() -> Redis:
        if url:
            return Redis.from_url(url, decode_responses=True)
        return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

    return crear


@pytest.fixture(autouse=True)
async def limpiar(redis_factory):
    EFECTOS_SECUNDARIOS.clear()
    if os.environ.get("REDIS_TEST_URL"):
        await redis_factory().flushdb()
    yield


@asynccontextmanager
async def api(redis_factory, *, llm=None, saver=None, **cambios):
    """Arranca la app completa (con su worker) y devuelve un cliente HTTP."""
    settings = Settings(checkpointer="memory", observability="none", **cambios)
    app = create_app(settings, redis_client=redis_factory(), checkpointer=saver or InMemorySaver(), llm=llm or PolicyLLM())
    async with app.router.lifespan_context(app):
        transporte = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transporte, base_url="http://test") as cliente:
            yield cliente


async def esperar_estado(cliente: httpx.AsyncClient, job_id: str, estados: set[str], timeout: float = 10) -> dict[str, Any]:
    fin = asyncio.get_running_loop().time() + timeout
    while True:
        job = (await cliente.get(f"/tasks/{job_id}")).json()
        if job["status"] in estados:
            return job
        assert asyncio.get_running_loop().time() < fin, f"Timeout esperando {estados}; estado actual: {job['status']}"
        await asyncio.sleep(0.05)
