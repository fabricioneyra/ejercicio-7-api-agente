"""Agente Ejecutor: realiza acciones con EFECTOS SECUNDARIOS (enviar un email,
emitir un reembolso). Son las únicas acciones "críticas" del sistema, por eso el
grafo no tiene ninguna arista que llegue a este agente sin pasar antes por el
nodo de aprobación humana (ver `hitl.py` y `graph.py`).

Las acciones están simuladas: no envían nada real, solo se registran en
`EFECTOS_SECUNDARIOS`, lo que permite comprobar en los tests que NO se ejecutaron
cuando el humano rechazó la acción.
"""

import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

EXECUTOR_PROMPT = (
    "Sos el AGENTE EJECUTOR. Realizás UNA acción con efectos reales usando tus "
    "herramientas, exactamente como lo indica la instrucción y con los datos que "
    "recibís (no inventes montos, destinatarios ni ids). Si falta algún dato "
    "imprescindible, no ejecutes nada y decí qué falta. Cuando termines, informá en "
    "español qué acción hiciste y su identificador. Esta acción ya fue aprobada por "
    "una persona."
)

# Registro de acciones ejecutadas (simulación de efectos secundarios).
EFECTOS_SECUNDARIOS: list[dict[str, Any]] = []


@tool
def enviar_reporte_por_email(destinatario: str, asunto: str, cuerpo: str) -> dict[str, Any]:
    """Envía un email con un reporte a una persona. ACCIÓN CON EFECTO SECUNDARIO:
    el mensaje sale de verdad y no se puede deshacer.

    Usala solo cuando se pida explícitamente enviar o mandar un reporte por email.
    Recibe el email del destinatario, el asunto y el cuerpo (texto completo, con los
    datos concretos que se quieren informar). Devuelve {"enviado": True, "mensaje_id": ...}.
    """
    registro = {
        "accion": "enviar_reporte_por_email",
        "destinatario": destinatario,
        "asunto": asunto,
        "mensaje_id": f"msg-{uuid.uuid4().hex[:8]}",
    }
    EFECTOS_SECUNDARIOS.append(registro)
    return {"enviado": True, "mensaje_id": registro["mensaje_id"]}


@tool
def emitir_reembolso(cliente_id: int, monto: float, motivo: str) -> dict[str, Any]:
    """Emite un reembolso de dinero a un cliente. ACCIÓN CON EFECTO SECUNDARIO Y
    COSTO ECONÓMICO: mueve dinero y no se puede deshacer.

    Usala solo cuando se pida explícitamente reembolsar o devolver dinero. Recibe el
    número de cliente, el monto en pesos y el motivo. Devuelve
    {"reembolsado": True, "operacion_id": ...}.
    """
    registro = {
        "accion": "emitir_reembolso",
        "cliente_id": cliente_id,
        "monto": monto,
        "motivo": motivo,
        "operacion_id": f"op-{uuid.uuid4().hex[:8]}",
    }
    EFECTOS_SECUNDARIOS.append(registro)
    return {"reembolsado": True, "operacion_id": registro["operacion_id"]}


EXECUTOR_TOOLS = [enviar_reporte_por_email, emitir_reembolso]


def create_executor_agent(llm: BaseChatModel) -> CompiledStateGraph:
    return create_react_agent(llm, EXECUTOR_TOOLS, prompt=EXECUTOR_PROMPT, name="ejecutor")
