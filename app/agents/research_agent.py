"""Agente de Investigación: consulta la base de conocimiento y devuelve datos
con su fuente. NO calcula ni interpreta: de eso se encarga el analista."""

import re
import unicodedata
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from app.data_base import DOCUMENTOS

RESEARCH_PROMPT = (
    "Sos el AGENTE DE INVESTIGACIÓN. Tu única tarea es buscar información en la "
    "base de conocimiento con tus herramientas y devolverla tal cual, "
    "citando la fuente (campo 'fuente') y el id de cada documento. "
    "No calcules promedios ni analices el sentimiento: eso lo hace otro agente. "
    "Devolvé los datos CRUDOS y completos que se pidieron (por ejemplo, la lista "
    "de reseñas con su calificación). Si no encontrás nada, decilo explícitamente. "
    "Respondé en español."
)


def _normalizar(texto: str) -> set[str]:
    sin_acentos = unicodedata.normalize("NFD", texto.lower())
    sin_acentos = "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn")
    return {p for p in re.findall(r"[a-z0-9]+", sin_acentos) if len(p) > 2}


@tool
def buscar_conocimiento(consulta: str, max_resultados: int = 10) -> list[dict[str, Any]]:
    """Busca documentos en la base de conocimiento de BankLab (reseñas de clientes
    y fichas oficiales de productos como "Tarjeta Plus" o "Cuenta Sueldo Digital").

    Usala para obtener datos CRUDOS: reseñas con su calificación (1 a 5), comisiones,
    costos y condiciones de un producto. Pasá el nombre del producto y, si hace falta,
    el tipo de dato (ej. "Tarjeta Plus reseñas", "Tarjeta Plus comisión").

    Devuelve una lista de documentos con: id, fuente, producto, tipo ("resena" o
    "ficha"), texto y calificacion (None en las fichas). Si no hay coincidencias,
    devuelve una lista vacía. No calcula nada: solo recupera.
    """
    palabras = _normalizar(consulta)
    puntuados: list[tuple[int, dict[str, Any]]] = []
    for doc in DOCUMENTOS:
        contenido = _normalizar(f"{doc['producto']} {doc['tipo']} {doc['texto']}")
        puntaje = len(palabras & contenido)
        # "reseñas"/"comisión" no coinciden literalmente con "resena"/"ficha":
        # el tipo suma si la consulta lo menciona.
        if doc["tipo"] == "resena" and palabras & {"resenas", "resena", "opiniones", "comentarios"}:
            puntaje += 2
        if doc["tipo"] == "ficha" and palabras & {"comision", "costo", "costos", "precio", "tarifa", "ficha"}:
            puntaje += 2
        if puntaje > 0 and palabras & _normalizar(doc["producto"]):
            puntuados.append((puntaje, dict(doc)))
    puntuados.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in puntuados[:max_resultados]]


RESEARCH_TOOLS = [buscar_conocimiento]


def create_research_agent(llm: BaseChatModel) -> CompiledStateGraph:
    return create_react_agent(
        llm, RESEARCH_TOOLS, prompt=RESEARCH_PROMPT, name="investigador"
    )
