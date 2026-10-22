"""Estado compartido del orquestador (igual que el Módulo 6, más la aprobación humana)."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import MessagesState


class Contribucion(TypedDict):
    """Un aporte firmado de un especialista."""

    paso: int
    agente: str  # "investigador" | "analista" | "ejecutor" | "aprobacion_humana"
    instruccion: str
    resultado: str
    herramientas: list[str]


class Evaluacion(TypedDict):
    datos_con_fuente: bool
    calculos_hechos_por_analista: bool
    responde_toda_la_consulta: bool


class DecisionRegistrada(TypedDict):
    paso: int
    siguiente: str
    razon: str
    instruccion: str
    evaluacion: Evaluacion


class Aprobacion(TypedDict):
    """Respuesta del humano a una acción crítica."""

    aprobada: bool
    comentario: str


class OrchestratorState(MessagesState):
    next_agent: str  # investigador | analista | ejecutor | FINISH
    instruccion: str
    pasos: int
    contribuciones: Annotated[list[Contribucion], operator.add]
    decisiones: Annotated[list[DecisionRegistrada], operator.add]
    aprobacion: Aprobacion | None  # decisión humana sobre la acción crítica pendiente
    task_completed: bool
