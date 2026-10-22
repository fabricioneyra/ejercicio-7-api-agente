"""Base de conocimiento simulada (reemplaza a una Vector DB / búsqueda web).

Reseñas y fichas de productos de la fintech ficticia "BankLab". La búsqueda se
hace por coincidencia de palabras clave: alcanza para el ejercicio y no necesita
claves de API ni red.
"""

from typing import TypedDict


class Documento(TypedDict):
    id: str
    fuente: str
    producto: str
    tipo: str  # "resena" | "ficha"
    texto: str
    calificacion: int | None  # 1-5 en reseñas, None en fichas


DOCUMENTOS: list[Documento] = [
    # --- Tarjeta Plus: reseñas ---
    {"id": "tp-r1", "fuente": "reseñas app BankLab", "producto": "Tarjeta Plus", "tipo": "resena", "calificacion": 5,
     "texto": "Excelente tarjeta, el cashback llega rápido y la app es muy clara. Recomiendo."},
    {"id": "tp-r2", "fuente": "reseñas app BankLab", "producto": "Tarjeta Plus", "tipo": "resena", "calificacion": 4,
     "texto": "Buena tarjeta, me gusta el cashback aunque el límite inicial es bajo."},
    {"id": "tp-r3", "fuente": "reseñas app BankLab", "producto": "Tarjeta Plus", "tipo": "resena", "calificacion": 2,
     "texto": "Pésimo soporte, tardaron una semana en responder y la comisión de mantenimiento es muy cara."},
    {"id": "tp-r4", "fuente": "foro de usuarios", "producto": "Tarjeta Plus", "tipo": "resena", "calificacion": 5,
     "texto": "Me encanta, los pagos con la tarjeta son rápidos y seguros. Muy buena experiencia."},
    {"id": "tp-r5", "fuente": "foro de usuarios", "producto": "Tarjeta Plus", "tipo": "resena", "calificacion": 3,
     "texto": "Está bien, pero la comisión es cara para lo que ofrece."},
    # --- Tarjeta Plus: ficha ---
    {"id": "tp-f1", "fuente": "ficha oficial BankLab", "producto": "Tarjeta Plus", "tipo": "ficha", "calificacion": None,
     "texto": "Tarjeta Plus: cuota de emisión única de $12000 y comisión de mantenimiento de $3500 por mes. Cashback del 2% en supermercados."},
    # --- Cuenta Sueldo Digital: reseñas ---
    {"id": "cs-r1", "fuente": "reseñas app BankLab", "producto": "Cuenta Sueldo Digital", "tipo": "resena", "calificacion": 5,
     "texto": "Excelente cuenta, sin costos y la transferencia es instantánea. Muy buena."},
    {"id": "cs-r2", "fuente": "reseñas app BankLab", "producto": "Cuenta Sueldo Digital", "tipo": "resena", "calificacion": 4,
     "texto": "Buena cuenta, simple y gratuita. Falta poder abrirla con más rapidez."},
    {"id": "cs-r3", "fuente": "foro de usuarios", "producto": "Cuenta Sueldo Digital", "tipo": "resena", "calificacion": 1,
     "texto": "Horrible, la app se cayó el día de cobro y no pude acceder a mi dinero. Muy malo."},
    # --- Cuenta Sueldo Digital: ficha ---
    {"id": "cs-f1", "fuente": "ficha oficial BankLab", "producto": "Cuenta Sueldo Digital", "tipo": "ficha", "calificacion": None,
     "texto": "Cuenta Sueldo Digital: sin cuota de emisión ni comisión de mantenimiento ($0). Transferencias gratuitas e instantáneas."},
]
