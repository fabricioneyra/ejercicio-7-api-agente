"""Prueba de carga: lanza N peticiones CONCURRENTES contra la API y mide latencias.

    python -m scripts.load_test                       # 5 peticiones contra localhost:8000
    python -m scripts.load_test --n 5 --url http://localhost:8000

Qué hace:
1. Envía las N consultas a la vez (asyncio.gather) con POST /tasks.
2. Consulta GET /tasks/{id} hasta que cada job termina (DONE o FAILED).
3. Imprime latencia por job (de extremo a extremo, medida desde el cliente), p50, p95,
   máximo y cuántos terminaron bien. Guarda todo en load_test_results.json, con los
   job_id y la ventana de tiempo, para ubicar esas trazas en el dashboard.

El COSTO por ejecución no se calcula acá: lo calcula Phoenix/LangSmith a partir de los
tokens de entrada y salida de cada traza. Mirá el dashboard y capturalo.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

PREGUNTAS = [
    "Investigá las reseñas de la Tarjeta Plus, calculá su calificación promedio y el porcentaje de reseñas positivas.",
    "Investigá las reseñas de la Cuenta Sueldo Digital y calculá su calificación promedio y el porcentaje de reseñas positivas.",
    "Calculá cuánto cuesta tener la Tarjeta Plus durante 3 años (cuota de emisión más comisión mensual) y si conviene según las reseñas.",
    "Compará la Tarjeta Plus y la Cuenta Sueldo Digital: calificación promedio de cada una y costo a 2 años.",
    "¿Cuál de los dos productos, Tarjeta Plus o Cuenta Sueldo Digital, tiene mayor porcentaje de reseñas positivas? Justificá con cálculos.",
]
TERMINALES = {"DONE", "FAILED"}


def percentil(valores: list[float], p: float) -> float:
    """Percentil con interpolación lineal (igual que numpy.percentile por defecto)."""
    orden = sorted(valores)
    if len(orden) == 1:
        return orden[0]
    pos = (len(orden) - 1) * p / 100
    bajo, alto = int(pos), min(int(pos) + 1, len(orden) - 1)
    return orden[bajo] + (orden[alto] - orden[bajo]) * (pos - bajo)


async def correr_job(cliente: httpx.AsyncClient, pregunta: str, args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.perf_counter()
    inicio = datetime.now(timezone.utc)
    r = await cliente.post("/tasks", json={"pregunta": pregunta})
    r.raise_for_status()
    job_id = r.json()["job_id"]
    t_encolado = time.perf_counter() - t0  # lo que tarda la API en responder

    aprobado = False
    while True:
        job = (await cliente.get(f"/tasks/{job_id}")).json()
        if job["status"] in TERMINALES:
            break
        if job["status"] == "AWAITING_APPROVAL" and args.auto_approve and not aprobado:
            await cliente.post(f"/tasks/{job_id}/approve", json={"approved": True, "comentario": "load test"})
            aprobado = True
        if time.perf_counter() - t0 > args.timeout:
            job["status"] = "TIMEOUT_CLIENTE"
            break
        await asyncio.sleep(args.poll)

    return {
        "job_id": job_id,
        "pregunta": pregunta,
        "status": job["status"],
        "error": job.get("error"),
        "latencia_extremo_a_extremo_s": round(time.perf_counter() - t0, 2),
        "tiempo_de_respuesta_post_s": round(t_encolado, 3),
        "inicio_utc": inicio.isoformat(timespec="seconds"),
        "flujo": (job.get("resultado") or {}).get("flujo"),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba de carga con peticiones concurrentes.")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--n", type=int, default=5, help="Cantidad de peticiones concurrentes.")
    parser.add_argument("--poll", type=float, default=1.0, help="Segundos entre consultas de estado.")
    parser.add_argument("--timeout", type=float, default=300, help="Máximo de espera por job (s).")
    parser.add_argument("--auto-approve", action="store_true", help="Aprueba solo las pausas HITL.")
    parser.add_argument("--out", default="load_test_results.json")
    args = parser.parse_args()

    preguntas = [PREGUNTAS[i % len(PREGUNTAS)] for i in range(args.n)]
    print(f"Lanzando {args.n} peticiones CONCURRENTES a {args.url} ...")
    inicio = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as cliente:
        try:
            await cliente.get("/health")
        except httpx.HTTPError as error:
            print(f"No se pudo conectar a la API en {args.url}: {error}\nLevantala primero (ver README).")
            return 2
        resultados = await asyncio.gather(*[correr_job(cliente, q, args) for q in preguntas])
    total = time.perf_counter() - t0
    fin = datetime.now(timezone.utc)

    latencias = [r["latencia_extremo_a_extremo_s"] for r in resultados]
    resumen = {
        "peticiones": args.n,
        "terminaron_ok": sum(r["status"] == "DONE" for r in resultados),
        "fallidas": sum(r["status"] != "DONE" for r in resultados),
        "tiempo_total_s": round(total, 2),
        "latencia_media_s": round(statistics.fmean(latencias), 2),
        "latencia_p50_s": round(percentil(latencias, 50), 2),
        "latencia_p95_s": round(percentil(latencias, 95), 2),
        "latencia_max_s": round(max(latencias), 2),
        "ventana_utc": [inicio.isoformat(timespec="seconds"), fin.isoformat(timespec="seconds")],
    }

    print(f"\n{'job_id':<34} {'estado':<10} {'latencia (s)':>12}")
    for r in resultados:
        print(f"{r['job_id']:<34} {r['status']:<10} {r['latencia_extremo_a_extremo_s']:>12}")
    print("\nResumen (latencias medidas desde el cliente):")
    for clave, valor in resumen.items():
        print(f"  {clave:<20} {valor}")
    print("\nCon n pequeño, p95 queda muy cerca del máximo. El costo por ejecución y la latencia "
          "que muestra la plataforma están en el dashboard (Phoenix/LangSmith).")

    Path(args.out).write_text(json.dumps({"resumen": resumen, "jobs": resultados}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Resultados guardados en {args.out}")
    return 0 if resumen["fallidas"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
