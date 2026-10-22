"""Agente de Análisis/Cómputo: procesa los datos que trajo el investigador
(cálculos, estadísticas y análisis de sentimiento). No busca información."""

import ast
import operator
import re
import statistics
import unicodedata
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

ANALYST_PROMPT = (
    "Sos el AGENTE DE ANÁLISIS. Recibís datos ya recolectados y los procesás con "
    "tus herramientas: NUNCA hagas cuentas ni estimes el sentimiento 'de cabeza', "
    "usá siempre las herramientas y reportá sus resultados. No tenés acceso a la "
    "base de conocimiento: trabajá solo con los datos que te pasan. Si faltan datos "
    "para calcular algo, decí exactamente cuáles faltan. Respondé en español, con "
    "los números claros."
)

# --- calculadora segura (solo aritmética, sin eval) ---------------------------
_OPERADORES = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.Mod: operator.mod,
}


def _evaluar(nodo: ast.AST) -> float:
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, (int, float)):
        return nodo.value
    if isinstance(nodo, ast.BinOp) and type(nodo.op) in _OPERADORES:
        return _OPERADORES[type(nodo.op)](_evaluar(nodo.left), _evaluar(nodo.right))
    if isinstance(nodo, ast.UnaryOp) and type(nodo.op) in _OPERADORES:
        return _OPERADORES[type(nodo.op)](_evaluar(nodo.operand))
    raise ValueError("Solo se permiten números y operadores + - * / ** %")


@tool
def calcular(expresion: str) -> dict[str, Any]:
    """Calcula una expresión aritmética exacta. Usala para CUALQUIER cuenta
    (totales, costos acumulados, porcentajes) en lugar de calcular mentalmente.

    Recibe un string con números y operadores + - * / ** % y paréntesis, sin
    unidades ni símbolos de moneda. Ejemplo: "12000 + 3500 * 36".
    Devuelve {"resultado": número}. Si la expresión no es válida, devuelve un
    campo "error": corregí la expresión y volvé a intentar.
    """
    try:
        return {"expresion": expresion, "resultado": _evaluar(ast.parse(expresion, mode="eval").body)}
    except (ValueError, SyntaxError, ZeroDivisionError) as error:
        return {"error": f"Expresión inválida ({error}). Usá solo números y + - * / ** %."}


@tool
def estadisticas_basicas(valores: list[float]) -> dict[str, Any]:
    """Calcula estadísticas descriptivas de una lista de números: cantidad,
    promedio, mediana, mínimo y máximo. Usala, por ejemplo, para obtener la
    calificación promedio a partir de las calificaciones (1 a 5) de las reseñas.

    Recibe una lista de números (al menos uno). Devuelve un diccionario con
    cantidad, promedio, mediana, minimo y maximo (promedio redondeado a 2
    decimales). Si la lista está vacía, devuelve un campo "error".
    """
    if not valores:
        return {"error": "La lista de valores está vacía."}
    return {
        "cantidad": len(valores),
        "promedio": round(statistics.fmean(valores), 2),
        "mediana": statistics.median(valores),
        "minimo": min(valores),
        "maximo": max(valores),
    }


# --- sentimiento por léxico (simple y determinista) ---------------------------
_POSITIVAS = {"excelente", "buena", "bueno", "buenisima", "encanta", "recomiendo", "rapido", "rapida",
              "seguro", "seguros", "clara", "claro", "gratuita", "gratis", "instantanea", "simple", "genial", "util", "gusta", "bien"}
_NEGATIVAS = {"pesimo", "pesima", "horrible", "malo", "mala", "cara", "caro", "tardaron", "bajo",
              "cayo", "problema", "lento", "lenta", "falla", "fallas", "mal"}


def _tokens(texto: str) -> list[str]:
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return re.findall(r"[a-z]+", sin_acentos)


@tool
def analizar_sentimiento(textos: list[str]) -> dict[str, Any]:
    """Analiza el sentimiento (positivo, negativo o neutro) de una lista de textos
    en español, por ejemplo reseñas de clientes. Usala cuando haya que saber si
    las opiniones son favorables o desfavorables.

    Recibe una lista de strings (los textos de las reseñas, SIN calificaciones).
    Devuelve, por cada texto, su etiqueta y puntaje (positivas menos negativas), y
    un resumen: cantidad de positivos, negativos y neutros y el porcentaje de
    positivos. Si la lista está vacía, devuelve un campo "error".
    """
    if not textos:
        return {"error": "No se recibieron textos para analizar."}
    detalle = []
    for texto in textos:
        palabras = _tokens(texto)
        puntaje = sum(p in _POSITIVAS for p in palabras) - sum(p in _NEGATIVAS for p in palabras)
        etiqueta = "positivo" if puntaje > 0 else "negativo" if puntaje < 0 else "neutro"
        detalle.append({"texto": texto[:60], "etiqueta": etiqueta, "puntaje": puntaje})
    conteo = {e: sum(d["etiqueta"] == e for d in detalle) for e in ("positivo", "negativo", "neutro")}
    return {
        "detalle": detalle,
        "resumen": {**conteo, "porcentaje_positivos": round(100 * conteo["positivo"] / len(textos), 1)},
    }


ANALYST_TOOLS = [calcular, estadisticas_basicas, analizar_sentimiento]


def create_analyst_agent(llm: BaseChatModel) -> CompiledStateGraph:
    return create_react_agent(llm, ANALYST_TOOLS, prompt=ANALYST_PROMPT, name="analista")
