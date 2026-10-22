"""Human-in-the-loop (HITL): pausa obligatoria antes de cualquier acción crítica.

Cómo funciona
1. El supervisor decide que hay que ejecutar una acción con efectos reales
   (enviar un email, emitir un reembolso) -> la arista lo manda a `aprobacion_humana`.
2. `aprobacion_humana` llama a `interrupt(...)`: LangGraph guarda el estado en el
   checkpointer (Redis) y DETIENE la ejecución. El worker marca el job como
   AWAITING_APPROVAL y queda libre para atender otros jobs.
3. Una persona llama a `POST /tasks/{id}/approve`. Eso encola la reanudación y el
   worker continúa con `Command(resume=...)`. Si aprobó, pasa al ejecutor; si
   rechazó, vuelve al supervisor sin ejecutar nada.
"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import AIMessage
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.jobs import Status
from app.state import OrchestratorState

router = APIRouter(tags=["human-in-the-loop"])


# --- nodo del grafo -----------------------------------------------------------
async def aprobacion_humana(state: OrchestratorState) -> dict[str, Any]:
    """Pausa el grafo hasta recibir una decisión externa.

    Importante: al reanudar, LangGraph vuelve a ejecutar este nodo desde el
    principio y `interrupt()` devuelve la respuesta del humano. Por eso antes de
    `interrupt()` no hay ningún efecto secundario.
    """
    decision: dict[str, Any] = interrupt(
        {
            "tipo": "aprobacion_requerida",
            "accion_solicitada": state["instruccion"],
            "razon_del_supervisor": state["decisiones"][-1]["razon"] if state.get("decisiones") else "",
            "como_responder": "POST /tasks/{job_id}/approve con {\"approved\": true|false, \"comentario\": \"...\"}",
        }
    )
    aprobada = bool(decision.get("approved"))
    comentario = str(decision.get("comentario", ""))

    if aprobada:
        return {
            "aprobacion": {"aprobada": True, "comentario": comentario},
            "messages": [
                AIMessage(name="aprobacion_humana", content=f"Acción APROBADA por una persona. {comentario}".strip())
            ],
        }
    texto = f"Acción RECHAZADA por una persona: NO se ejecutó. {comentario}".strip()
    return {
        "aprobacion": {"aprobada": False, "comentario": comentario},
        "contribuciones": [
            {
                "paso": state["pasos"],
                "agente": "aprobacion_humana",
                "instruccion": state["instruccion"],
                "resultado": texto,
                "herramientas": [],
            }
        ],
        "messages": [AIMessage(name="aprobacion_humana", content=texto)],
    }


def route_aprobacion(state: OrchestratorState) -> Literal["ejecutor", "supervisor"]:
    aprobacion = state.get("aprobacion")
    return "ejecutor" if aprobacion and aprobacion["aprobada"] else "supervisor"


# --- endpoint -----------------------------------------------------------------
class ApprovalRequest(BaseModel):
    approved: bool = Field(description="true para autorizar la acción, false para rechazarla.")
    comentario: str = Field(default="", max_length=500)


@router.post("/tasks/{job_id}/approve", status_code=202)
async def approve_task(job_id: str, body: ApprovalRequest, request: Request) -> dict[str, Any]:
    """Aprueba o rechaza una acción crítica pendiente. No bloquea: encola la
    reanudación y devuelve de inmediato; el avance se ve con `GET /tasks/{id}`."""
    store = request.app.state.store
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(404, f"No existe el job {job_id}.")
    if job["status"] != Status.AWAITING_APPROVAL:
        raise HTTPException(
            409, f"El job está en estado {job['status']}; solo se puede decidir cuando está {Status.AWAITING_APPROVAL}."
        )

    decision = {"approved": body.approved, "comentario": body.comentario}
    # Transición atómica: si dos personas aprueban a la vez, solo una gana.
    if not await store.transition(
        job_id, Status.AWAITING_APPROVAL, Status.PENDING, decision_humana=decision
    ):
        raise HTTPException(409, "Otra solicitud ya resolvió esta aprobación.")
    await store.enqueue({"job_id": job_id, "action": "resume", "decision": decision})
    return {"job_id": job_id, "status": Status.PENDING, "decision": decision}
