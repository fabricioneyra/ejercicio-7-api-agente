"""Grafo del orquestador multi-agente con Human-in-the-loop.

    START -> supervisor --(investigador)--> investigador ---\\
               ^  |  \\---(analista)-------> analista -------+--> supervisor
               |  |   \\--(ejecutor)--> aprobacion_humana --(aprobada)--> ejecutor --> supervisor
               |  |                          \\--(rechazada)--> supervisor
               |  \\---(FINISH)--> sintesis --> END
               +-----------------------------------------------/
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.analyst_agent import create_analyst_agent
from app.agents.executor_agent import create_executor_agent
from app.agents.research_agent import create_research_agent
from app.hitl import aprobacion_humana, route_aprobacion
from app.state import Contribucion, OrchestratorState
from app.supervisor import crear_supervisor, pregunta_del_usuario, resumen_del_equipo, route_supervisor

# Techo de pasos del grafo por ejecución (evita bucles y costos inesperados).
RECURSION_LIMIT = 30
SUBAGENT_RECURSION_LIMIT = 12

SINTESIS_PROMPT = (
    "Sos el redactor final de un equipo de análisis. Con la consulta del usuario y los "
    "aportes del equipo, escribí la respuesta final en español: clara, breve y organizada. "
    "Usá SOLO los datos y cálculos de los aportes (no inventes ni recalcules). Citá la "
    "fuente de los datos cuando la tengan. Si hubo una acción pedida, indicá claramente "
    "si se ejecutó, o si una persona la rechazó y por lo tanto NO se ejecutó."
)


def create_llm(model: str) -> BaseChatModel:
    return ChatOpenAI(model=model, temperature=0)


def _contexto_para(agente: str, state: OrchestratorState) -> str:
    """Contexto mínimo por especialista (evita la contaminación de contexto)."""
    fuentes = {"analista": {"investigador"}, "ejecutor": {"investigador", "analista"}}.get(agente)
    if not fuentes:
        return ""
    return "\n\n".join(c["resultado"] for c in state.get("contribuciones", []) if c["agente"] in fuentes)


def _crear_nodo_especialista(nombre: str, agente: CompiledStateGraph):
    async def nodo(state: OrchestratorState) -> dict[str, Any]:
        instruccion = state["instruccion"]
        contexto = _contexto_para(nombre, state)
        mensaje = instruccion
        if contexto:
            mensaje += f"\n\nDATOS DISPONIBLES (aportados por otros agentes):\n{contexto}"

        herramientas: list[str] = []
        try:
            resultado = await agente.ainvoke(
                {"messages": [HumanMessage(content=mensaje)]},
                {"recursion_limit": SUBAGENT_RECURSION_LIMIT},
            )
            mensajes = resultado["messages"]
            texto = str(mensajes[-1].content)
            herramientas = [tc["name"] for m in mensajes if isinstance(m, AIMessage) for tc in m.tool_calls]
        except GraphRecursionError:
            texto = "ERROR: el especialista no logró terminar dentro del límite de pasos."

        contribucion: Contribucion = {
            "paso": state["pasos"],
            "agente": nombre,
            "instruccion": instruccion,
            "resultado": texto,
            "herramientas": herramientas,
        }
        return {"contribuciones": [contribucion], "messages": [AIMessage(content=texto, name=nombre)]}

    return nodo


def _crear_nodo_sintesis(llm: BaseChatModel):
    async def sintesis(state: OrchestratorState) -> dict[str, Any]:
        respuesta = await llm.ainvoke(
            [
                SystemMessage(content=SINTESIS_PROMPT),
                HumanMessage(
                    content=(
                        f"CONSULTA DEL USUARIO:\n{pregunta_del_usuario(state)}\n\n"
                        f"APORTES DEL EQUIPO:\n{resumen_del_equipo(state)}"
                    )
                ),
            ]
        )
        return {"messages": [AIMessage(content=respuesta.content, name="sintesis")], "task_completed": True}

    return sintesis


def build_graph(llm: BaseChatModel, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """Compila el grafo con el checkpointer (Redis) que persiste el estado por thread_id."""
    graph = StateGraph(OrchestratorState)
    graph.add_node("supervisor", crear_supervisor(llm))
    graph.add_node("investigador", _crear_nodo_especialista("investigador", create_research_agent(llm)))
    graph.add_node("analista", _crear_nodo_especialista("analista", create_analyst_agent(llm)))
    graph.add_node("aprobacion_humana", aprobacion_humana)
    graph.add_node("ejecutor", _crear_nodo_especialista("ejecutor", create_executor_agent(llm)))
    graph.add_node("sintesis", _crear_nodo_sintesis(llm))

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_supervisor,
        {
            "investigador": "investigador",
            "analista": "analista",
            "aprobacion_humana": "aprobacion_humana",
            "sintesis": "sintesis",
        },
    )
    graph.add_edge("investigador", "supervisor")
    graph.add_edge("analista", "supervisor")
    graph.add_conditional_edges(
        "aprobacion_humana", route_aprobacion, {"ejecutor": "ejecutor", "supervisor": "supervisor"}
    )
    graph.add_edge("ejecutor", "supervisor")
    graph.add_edge("sintesis", END)
    return graph.compile(checkpointer=checkpointer)


def estado_inicial(pregunta: str) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=pregunta)],
        "next_agent": "",
        "instruccion": "",
        "pasos": 0,
        "contribuciones": [],
        "decisiones": [],
        "aprobacion": None,
        "task_completed": False,
    }
