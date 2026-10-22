"""Pruebas de la API: cola asíncrona, estado en Redis, FAILED, HITL y persistencia."""

import asyncio
import time

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.executor_agent import EFECTOS_SECUNDARIOS
from tests.conftest import PolicyLLM, api, esperar_estado

TERMINALES = {"DONE", "FAILED"}


async def test_encola_y_devuelve_job_id_y_completa(redis_factory):
    async with api(redis_factory) as c:
        r = await c.post("/tasks", json={"pregunta": "¿Conviene la Tarjeta Plus?"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        assert r.json()["status"] == "PENDING"

        job = await esperar_estado(c, job_id, TERMINALES)
        assert job["status"] == "DONE", job
        assert job["resultado"]["flujo"] == ["investigador", "analista", "FINISH"]
        assert job["resultado"]["respuesta"] == "Respuesta final del equipo."
        assert [a["agente"] for a in job["resultado"]["aportes"]] == ["investigador", "analista"]
        assert job["duracion_segundos"] >= 0


async def test_valida_entrada_y_404(redis_factory):
    async with api(redis_factory) as c:
        assert (await c.post("/tasks", json={"pregunta": "a"})).status_code == 422
        assert (await c.get("/tasks/no-existe")).status_code == 404
        assert (await c.post("/tasks/no-existe/approve", json={"approved": True})).status_code == 404


async def test_endpoints_no_bloquean_el_event_loop(redis_factory):
    """Con 5 jobs corriendo (cada llamada al LLM tarda 0,3 s), la API sigue
    respondiendo al instante y los 5 avanzan en paralelo, no en serie."""
    async with api(redis_factory, llm=PolicyLLM(delay=0.3), worker_concurrency=5) as c:
        t0 = time.perf_counter()
        respuestas = await asyncio.gather(*[c.post("/tasks", json={"pregunta": f"Consulta número {i}"}) for i in range(5)])
        t_post = time.perf_counter() - t0
        assert all(r.status_code == 202 for r in respuestas)
        assert t_post < 0.25, f"POST /tasks tardó {t_post:.2f}s: se está bloqueando"

        # Mientras los jobs corren, /health responde rápido.
        t1 = time.perf_counter()
        assert (await c.get("/health")).status_code == 200
        assert time.perf_counter() - t1 < 0.25

        ids = [r.json()["job_id"] for r in respuestas]
        t2 = time.perf_counter()
        jobs = await asyncio.gather(*[esperar_estado(c, i, TERMINALES, timeout=20) for i in ids])
        total = time.perf_counter() - t2
        assert all(j["status"] == "DONE" for j in jobs)
        # Un job tarda ~12 llamadas * 0,3 s = ~3,6 s. En serie serían ~18 s.
        assert total < 9, f"5 jobs tardaron {total:.1f}s: no están corriendo en paralelo"


async def test_excepcion_en_segundo_plano_deja_el_job_en_failed(redis_factory):
    async with api(redis_factory, llm=PolicyLLM(fail_supervisor=True)) as c:
        job_id = (await c.post("/tasks", json={"pregunta": "Esto va a fallar"})).json()["job_id"]
        job = await esperar_estado(c, job_id, TERMINALES)
        assert job["status"] == "FAILED"
        assert "RuntimeError" in job["error"] and "boom" in job["error"]
        # El cliente nunca queda esperando: el estado terminal quedó persistido en Redis.
        assert (await c.get(f"/tasks/{job_id}")).json()["status"] == "FAILED"


async def test_timeout_marca_failed(redis_factory):
    async with api(redis_factory, llm=PolicyLLM(delay=2), job_timeout_seconds=1) as c:
        job_id = (await c.post("/tasks", json={"pregunta": "Consulta lenta"})).json()["job_id"]
        job = await esperar_estado(c, job_id, TERMINALES, timeout=8)
        assert job["status"] == "FAILED" and "Timeout" in job["error"]


async def test_hitl_pausa_hasta_aprobar_y_luego_ejecuta(redis_factory):
    async with api(redis_factory) as c:
        job_id = (await c.post("/tasks", json={"pregunta": "Calculá el promedio y enviá un reporte a Ana"})).json()["job_id"]

        job = await esperar_estado(c, job_id, {"AWAITING_APPROVAL", "FAILED", "DONE"})
        assert job["status"] == "AWAITING_APPROVAL", job
        assert job["aprobacion_pendiente"]["tipo"] == "aprobacion_requerida"
        assert "ana@example.com" in job["aprobacion_pendiente"]["accion_solicitada"]
        assert EFECTOS_SECUNDARIOS == [], "¡La acción crítica se ejecutó SIN aprobación!"

        # Se mantiene pausado: no avanza solo.
        await asyncio.sleep(0.5)
        assert (await c.get(f"/tasks/{job_id}")).json()["status"] == "AWAITING_APPROVAL"

        r = await c.post(f"/tasks/{job_id}/approve", json={"approved": True, "comentario": "ok"})
        assert r.status_code == 202
        # Una segunda decisión sobre el mismo job se rechaza.
        assert (await c.post(f"/tasks/{job_id}/approve", json={"approved": True})).status_code == 409

        job = await esperar_estado(c, job_id, TERMINALES)
        assert job["status"] == "DONE", job
        assert job["resultado"]["aprobacion"]["aprobada"] is True
        assert [e["accion"] for e in EFECTOS_SECUNDARIOS] == ["enviar_reporte_por_email"]
        assert job["resultado"]["flujo"] == ["investigador", "analista", "ejecutor", "FINISH"]


async def test_hitl_rechazo_no_ejecuta_nada(redis_factory):
    async with api(redis_factory) as c:
        job_id = (await c.post("/tasks", json={"pregunta": "Calculá el promedio y enviá un reporte a Ana"})).json()["job_id"]
        await esperar_estado(c, job_id, {"AWAITING_APPROVAL"})
        await c.post(f"/tasks/{job_id}/approve", json={"approved": False, "comentario": "no corresponde"})

        job = await esperar_estado(c, job_id, TERMINALES)
        assert job["status"] == "DONE", job
        assert job["resultado"]["aprobacion"] == {"aprobada": False, "comentario": "no corresponde"}
        assert EFECTOS_SECUNDARIOS == []
        assert job["resultado"]["flujo"][-1] == "FINISH"


async def test_aprobar_un_job_que_no_espera_da_409(redis_factory):
    async with api(redis_factory) as c:
        job_id = (await c.post("/tasks", json={"pregunta": "Consulta común sin acciones"})).json()["job_id"]
        await esperar_estado(c, job_id, TERMINALES)
        assert (await c.post(f"/tasks/{job_id}/approve", json={"approved": True})).status_code == 409


async def test_el_estado_sobrevive_a_un_reinicio(redis_factory):
    """El job pausado en HITL se reanuda en OTRA instancia de la app: el estado del job
    sale de Redis y el del grafo del checkpointer compartido."""
    saver = InMemorySaver()  # representa al checkpointer persistente (Redis)
    async with api(redis_factory, saver=saver) as c1:
        job_id = (await c1.post("/tasks", json={"pregunta": "Calculá el promedio y enviá un reporte a Ana"})).json()["job_id"]
        await esperar_estado(c1, job_id, {"AWAITING_APPROVAL"})
    # c1 y su worker ya se apagaron (reinicio del servidor).

    async with api(redis_factory, saver=saver) as c2:
        job = (await c2.get(f"/tasks/{job_id}")).json()
        assert job["status"] == "AWAITING_APPROVAL"
        await c2.post(f"/tasks/{job_id}/approve", json={"approved": True})
        job = await esperar_estado(c2, job_id, TERMINALES)
        assert job["status"] == "DONE" and len(EFECTOS_SECUNDARIOS) == 1
