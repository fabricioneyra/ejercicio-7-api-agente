"""Nodo Supervisor (del Módulo 6) con un tercer especialista, el `ejecutor`.

Frenos duros en código, independientes del LLM:
- `MAX_PASOS`: tope de decisiones del supervisor.
- `MAX_LLAMADAS_POR_AGENTE`: refinamientos máximos por especialista. El ejecutor
  tiene tope 1: una acción crítica se pide a aprobación una sola vez.
"""

from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.state import OrchestratorState

MAX_PASOS = 8
MAX_LLAMADAS_POR_AGENTE = {"investigador": 2, "analista": 2, "ejecutor": 1}
MAX_CARACTERES_CONTRIBUCION = 1500

SUPERVISOR_PROMPT = """\
Sos el SUPERVISOR de un equipo de especialistas. Dada la consulta del usuario y lo
que el equipo ya aportó, decidí quién debe intervenir ahora o si es momento de
finalizar.

ESPECIALISTAS
- investigador: busca datos en la base de conocimiento (reseñas con calificación,
  fichas con comisiones y costos). Solo recupera datos; no calcula.
- analista: calcula y analiza (promedios, sentimiento, costos acumulados). No tiene
  acceso a la base: solo trabaja con los datos que le pasás.
- ejecutor: realiza acciones con EFECTOS REALES (enviar un reporte por email, emitir un
  reembolso). Elegilo solo si el usuario pidió explícitamente una de esas acciones y
  ya están reunidos todos los datos necesarios (destinatario, monto, contenido).
  NO pidas confirmación al usuario: el sistema detiene automáticamente la ejecución y
  una persona debe aprobar la acción antes de que se realice. Si la persona la
  rechaza, no la vuelvas a intentar: elegí FINISH y explicá que no se ejecutó.

CÓMO DECIDIR
1. Si todavía faltan datos de la base -> investigador.
2. Si ya hay datos pero faltan cálculos o análisis -> analista, y en `instruccion`
   copiá los datos necesarios, completos, para que pueda trabajar sin más contexto.
3. Si el usuario pidió una acción con efectos reales y ya están los datos y cálculos
   -> ejecutor, con una instrucción completa (a quién, qué monto/contenido).
4. Si un aporte es incompleto o tiene errores, podés devolverlo al mismo especialista
   con una instrucción concreta de qué corregir (refinamiento), solo si hace falta.
5. Si la rúbrica se cumple, elegí FINISH.

RÚBRICA DE VALIDACIÓN (completá los tres campos con honestidad)
- datos_con_fuente: los datos usados vienen de la base e indican su fuente o id.
- calculos_hechos_por_analista: todo número derivado fue calculado por el analista
  con sus herramientas, no inventado.
- responde_toda_la_consulta: cada parte de la pregunta está cubierta (incluida la
  acción pedida, o la explicación de por qué no se ejecutó).
Solo podés elegir FINISH si los tres son verdaderos, o si no hay forma de mejorar el
resultado con las herramientas disponibles (explicalo en `razon`).

`instruccion` debe ser autocontenida: el especialista NO ve la conversación, solo esa
instrucción. Dejala vacía si elegís FINISH."""


class Decision(BaseModel):
    """Salida estructurada. La rúbrica va primero: el modelo evalúa antes de decidir."""

    razon: str = Field(description="Breve justificación de la decisión (1-2 frases).")
    datos_con_fuente: bool
    calculos_hechos_por_analista: bool
    responde_toda_la_consulta: bool
    siguiente: Literal["investigador", "analista", "ejecutor", "FINISH"]
    instruccion: str = Field(default="", description="Instrucción autocontenida.")


def pregunta_del_usuario(state: OrchestratorState) -> str:
    for mensaje in state["messages"]:
        if isinstance(mensaje, HumanMessage):
            return str(mensaje.content)
    return ""


def resumen_del_equipo(state: OrchestratorState) -> str:
    contribuciones = state.get("contribuciones", [])
    if not contribuciones:
        return "(todavía nadie aportó nada)"
    bloques = []
    for c in contribuciones:
        texto = c["resultado"]
        if len(texto) > MAX_CARACTERES_CONTRIBUCION:
            texto = texto[:MAX_CARACTERES_CONTRIBUCION] + "... [recortado]"
        bloques.append(
            f"[paso {c['paso']}] {c['agente']} (herramientas: "
            f"{', '.join(c['herramientas']) or 'ninguna'}):\n{texto}"
        )
    return "\n\n".join(bloques)


def _llamadas_por_agente(contribuciones: list[dict[str, Any]]) -> dict[str, int]:
    llamadas = {a: 0 for a in MAX_LLAMADAS_POR_AGENTE}
    for c in contribuciones:
        # Un rechazo humano consume el único intento del ejecutor.
        agente = "ejecutor" if c["agente"] == "aprobacion_humana" else c["agente"]
        if agente in llamadas:
            llamadas[agente] += 1
    return llamadas


def crear_supervisor(llm: BaseChatModel):
    decisor = llm.with_structured_output(Decision)

    async def supervisor(state: OrchestratorState) -> dict[str, Any]:
        paso = state.get("pasos", 0) + 1
        llamadas = _llamadas_por_agente(state.get("contribuciones", []))

        # Freno duro 1: tope de pasos. No se consulta al LLM.
        if paso > MAX_PASOS:
            decision = Decision(
                razon=f"Se alcanzó el tope de {MAX_PASOS} decisiones: se cierra con lo disponible.",
                datos_con_fuente=False,
                calculos_hechos_por_analista=False,
                responde_toda_la_consulta=False,
                siguiente="FINISH",
            )
        else:
            decision = await decisor.ainvoke(
                [
                    SystemMessage(content=SUPERVISOR_PROMPT),
                    HumanMessage(
                        content=(
                            f"CONSULTA DEL USUARIO:\n{pregunta_del_usuario(state)}\n\n"
                            f"APORTES DEL EQUIPO HASTA AHORA:\n{resumen_del_equipo(state)}\n\n"
                            f"Decisión número {paso} de un máximo de {MAX_PASOS}. "
                            f"Veces que intervino cada especialista: {llamadas}."
                        )
                    ),
                ]
            )

        siguiente, razon = decision.siguiente, decision.razon
        instruccion = decision.instruccion.strip()

        # Freno duro 2: límite de intervenciones por especialista.
        if siguiente != "FINISH" and llamadas[siguiente] >= MAX_LLAMADAS_POR_AGENTE[siguiente]:
            razon += f" [forzado a FINISH: '{siguiente}' ya alcanzó su límite de intervenciones]"
            siguiente = "FINISH"
        if siguiente != "FINISH" and not instruccion:
            instruccion = pregunta_del_usuario(state)

        registro = {
            "paso": paso,
            "siguiente": siguiente,
            "razon": razon,
            "instruccion": instruccion if siguiente != "FINISH" else "",
            "evaluacion": {
                "datos_con_fuente": decision.datos_con_fuente,
                "calculos_hechos_por_analista": decision.calculos_hechos_por_analista,
                "responde_toda_la_consulta": decision.responde_toda_la_consulta,
            },
        }
        return {
            "next_agent": siguiente,
            "instruccion": registro["instruccion"],
            "pasos": paso,
            "decisiones": [registro],
        }

    return supervisor


def route_supervisor(
    state: OrchestratorState,
) -> Literal["investigador", "analista", "aprobacion_humana", "sintesis"]:
    """Arista condicional. Nota: `ejecutor` NO tiene arista directa desde el
    supervisor; toda acción crítica pasa por `aprobacion_humana`."""
    siguiente = state["next_agent"]
    if siguiente == "investigador":
        return "investigador"
    if siguiente == "analista":
        return "analista"
    if siguiente == "ejecutor":
        return "aprobacion_humana"
    return "sintesis"
