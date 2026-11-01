"""Enrollment Service: matrícula y orquestación de la Saga (coreografía por eventos).

Flujo feliz:
  POST /enrollments -> reserva cupo en Course (REST, con Circuit Breaker)
                    -> estado PENDIENTE_PAGO -> evento 'matricula.creada'
  Payment publica 'pago.confirmado' -> estado CONFIRMADA -> evento 'matricula.confirmada'
Compensación:
  'pago.fallido' o tiempo de pago agotado -> libera el cupo en Course -> CANCELADA -> 'matricula.cancelada'
Aggregator:
  GET /enrollments/me/summary junta Student + Course + Payment + sus propias matrículas en una respuesta.
  Si alguno de los tres no responde, devuelve lo que tenga y lo lista en 'unavailable'.
Branch:
  Teacher consulta a Enrollment (rutas /internal/...) para saber quiénes están matriculados.
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from common.db import get_db
from common.errors import install_error_handlers
from common.limits import MAX_ID
from common.events import Outbox, start_consumer
from common.resilience import CircuitBreaker, CircuitOpenError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("enrollment")

DB = os.getenv("DB_PATH", "/data/enrollment.db")
COURSE_URL = os.getenv("COURSE_URL", "http://course:8000")
STUDENT_URL = os.getenv("STUDENT_URL", "http://student:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8000")
PAYMENT_TIMEOUT = int(os.getenv("PAYMENT_TIMEOUT_SECONDS", "300"))

outbox = Outbox(DB)
course_breaker = CircuitBreaker(failure_threshold=3, reset_timeout=10)
student_breaker = CircuitBreaker(failure_threshold=3, reset_timeout=10)
payment_breaker = CircuitBreaker(failure_threshold=3, reset_timeout=10)
http = httpx.Client(timeout=httpx.Timeout(3.0, connect=1.5))


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db(DB) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS enrollments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, student_id TEXT NOT NULL, course_id INTEGER NOT NULL, "
            "course_name TEXT, amount REAL, status TEXT NOT NULL, reason TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        # migración: bases de datos creadas antes de esta versión no tienen student_username
        cols = {r["name"] for r in c.execute("PRAGMA table_info(enrollments)")}
        if "student_username" not in cols:
            c.execute("ALTER TABLE enrollments ADD COLUMN student_username TEXT")
    outbox.init()


def _post_course(path: str, payload: dict) -> httpx.Response:
    r = http.post(f"{COURSE_URL}{path}", json=payload)
    if r.status_code >= 500:  # un 5xx cuenta como fallo del servicio para el Circuit Breaker
        raise httpx.HTTPError(f"Course respondió {r.status_code}")
    return r


def _cancel(enrollment_id: int, reason: str):
    """Cancela una matrícula que nunca llegó a tener cupo (no hay nada que compensar)."""
    with get_db(DB) as c:
        c.execute(
            "UPDATE enrollments SET status='CANCELADA', reason=?, updated_at=? WHERE id=?",
            (reason, time.time(), enrollment_id),
        )


def cancel_and_release(enrollment_id: int, reason: str):
    """Compensación de la Saga: libera el cupo y cancela. Si Course está caído, lanza error
    y el mensaje se reencola, así que la compensación se completa cuando Course vuelva."""
    with get_db(DB) as c:
        row = c.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row or row["status"] != "PENDIENTE_PAGO":
        return  # ya procesada (idempotencia)
    course_breaker.call(
        _post_course, f"/internal/courses/{row['course_id']}/release", {"enrollment_id": enrollment_id}
    )
    with get_db(DB) as c:
        updated = c.execute(
            "UPDATE enrollments SET status='CANCELADA', reason=?, updated_at=? "
            "WHERE id=? AND status='PENDIENTE_PAGO'",
            (reason, time.time(), enrollment_id),
        ).rowcount
        if updated:
            Outbox.add(c, "matricula.cancelada", {
                "enrollment_id": enrollment_id, "student_id": row["student_id"],
                "course_id": row["course_id"], "course_name": row["course_name"], "reason": reason,
            })
    outbox.flush_async()


def handle_event(routing_key: str, payload: dict):
    enrollment_id = payload.get("enrollment_id")
    if enrollment_id is None:
        return
    if routing_key == "pago.confirmado":
        with get_db(DB) as c:
            row = c.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
            if row and row["status"] == "PENDIENTE_PAGO":
                c.execute(
                    "UPDATE enrollments SET status='CONFIRMADA', updated_at=? WHERE id=?",
                    (time.time(), enrollment_id),
                )
                Outbox.add(c, "matricula.confirmada", {
                    "enrollment_id": enrollment_id, "student_id": row["student_id"],
                    "course_id": row["course_id"], "course_name": row["course_name"],
                })
        outbox.flush_async()
    elif routing_key == "pago.fallido":
        cancel_and_release(enrollment_id, "Pago fallido")


def expire_loop():
    """Cancela matrículas cuyo pago no llegó a tiempo."""
    while True:
        time.sleep(10)
        try:
            cutoff = time.time() - PAYMENT_TIMEOUT
            with get_db(DB) as c:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM enrollments WHERE status='PENDIENTE_PAGO' AND updated_at < ?", (cutoff,)
                )]
            for enrollment_id in ids:
                try:
                    cancel_and_release(enrollment_id, "Tiempo de pago agotado")
                except Exception as exc:
                    log.warning("No se pudo expirar la matrícula %s: %s", enrollment_id, exc)
        except Exception as exc:
            log.warning("Error en expire_loop: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_consumer("enrollment.q", ["pago.confirmado", "pago.fallido"], handle_event)
    outbox.start_flusher()
    threading.Thread(target=expire_loop, daemon=True, name="expire-loop").start()
    yield


app = FastAPI(title="Enrollment Service", lifespan=lifespan)
install_error_handlers(app)


class EnrollRequest(BaseModel):
    course_id: int = Field(ge=-MAX_ID, le=MAX_ID)


@app.get("/health")
def health():
    return {"service": "enrollment", "status": "ok", "course_breaker": course_breaker.state,
            "student_breaker": student_breaker.state, "payment_breaker": payment_breaker.state}


@app.post("/enrollments", status_code=201)
def create_enrollment(body: EnrollRequest, x_user_id: str = Header(...), x_user_role: str = Header(...),
                      x_user_name: str = Header("")):
    if x_user_role != "student":
        raise HTTPException(403, "Solo los estudiantes pueden matricularse")

    now = time.time()
    with get_db(DB) as c:
        dup = c.execute(
            "SELECT id FROM enrollments WHERE student_id=? AND course_id=? "
            "AND status IN ('SOLICITADA','PENDIENTE_PAGO','CONFIRMADA')",
            (x_user_id, body.course_id),
        ).fetchone()
        if dup:
            raise HTTPException(409, "Ya tienes una matrícula activa en este curso")
        cur = c.execute(
            "INSERT INTO enrollments(student_id, student_username, course_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'SOLICITADA', ?, ?)",
            (x_user_id, x_user_name, body.course_id, now, now),
        )
        enrollment_id = cur.lastrowid

    # Paso 1 de la Saga: reservar el cupo (llamada síncrona protegida por Circuit Breaker)
    try:
        r = course_breaker.call(
            _post_course, f"/internal/courses/{body.course_id}/reserve", {"enrollment_id": enrollment_id}
        )
    except CircuitOpenError:
        _cancel(enrollment_id, "Course no disponible (circuito abierto)")
        raise HTTPException(503, "El servicio de cursos no está disponible. Intenta más tarde.")
    except httpx.HTTPError:
        _cancel(enrollment_id, "Course no respondió")
        raise HTTPException(503, "El servicio de cursos no está disponible. Intenta más tarde.")

    if r.status_code == 404:
        _cancel(enrollment_id, "El curso no existe")
        raise HTTPException(404, "El curso no existe")
    if r.status_code == 409:
        _cancel(enrollment_id, "Sin cupos disponibles")
        raise HTTPException(409, "Sin cupos disponibles")
    if r.status_code != 200:
        _cancel(enrollment_id, f"Respuesta inesperada de Course ({r.status_code})")
        raise HTTPException(502, "Respuesta inesperada del servicio de cursos")

    course = r.json()
    with get_db(DB) as c:
        c.execute(
            "UPDATE enrollments SET status='PENDIENTE_PAGO', course_name=?, amount=?, updated_at=? WHERE id=?",
            (course["name"], course["price"], time.time(), enrollment_id),
        )
        # el evento se guarda en la misma transacción que el cambio de estado (Outbox)
        Outbox.add(c, "matricula.creada", {
            "enrollment_id": enrollment_id, "student_id": x_user_id, "course_id": body.course_id,
            "course_name": course["name"], "amount": course["price"],
        })
    outbox.flush_async()
    return {
        "id": enrollment_id, "course_id": body.course_id, "course_name": course["name"],
        "amount": course["price"], "status": "PENDIENTE_PAGO",
    }


@app.get("/enrollments")
def list_enrollments(x_user_id: str = Header(...), x_user_role: str = Header(...)):
    if x_user_role not in ("student", "admin"):
        raise HTTPException(403, "No tienes permiso para ver matrículas")
    with get_db(DB) as c:
        if x_user_role == "admin":
            rows = c.execute("SELECT * FROM enrollments ORDER BY id DESC LIMIT 200").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM enrollments WHERE student_id = ? ORDER BY id DESC", (x_user_id,)
            ).fetchall()
    return [dict(r) for r in rows]


def _fetch(name, breaker, url, params=None):
    """Una llamada del Aggregator. Devuelve (nombre, datos, respondió). Nunca lanza excepción."""
    def call():
        r = http.get(url, params=params)
        if r.status_code >= 500:
            raise httpx.HTTPError(f"{name} respondió {r.status_code}")
        return r

    try:
        r = breaker.call(call)
    except (CircuitOpenError, httpx.HTTPError):
        return name, None, False
    return name, (r.json() if r.status_code == 200 else None), True


@app.get("/enrollments/me/summary")
def my_summary(x_user_id: str = Header(...), x_user_role: str = Header(...)):
    """Patrón Aggregator: perfil (Student) + cursos (Course) + pagos pendientes (Payment)
    + matrículas propias, en una sola respuesta. Degrada si algún servicio no responde."""
    if x_user_role != "student":
        raise HTTPException(403, "Solo los estudiantes tienen resumen académico")
    with get_db(DB) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, course_id, course_name, amount, status, reason FROM enrollments "
            "WHERE student_id = ? ORDER BY id DESC", (x_user_id,))]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_fetch, "student", student_breaker, f"{STUDENT_URL}/internal/students/{x_user_id}"),
            pool.submit(_fetch, "courses", course_breaker, f"{COURSE_URL}/courses"),
            pool.submit(_fetch, "payments", payment_breaker, f"{PAYMENT_URL}/internal/payments",
                        {"student_id": x_user_id, "status": "PENDIENTE"}),
        ]
        results = {}
        for f in futures:
            name, data, responded = f.result()
            results[name] = (data, responded)
    courses_by_id = {c["id"]: c for c in (results["courses"][0] or [])}
    for e in rows:
        course = courses_by_id.get(e["course_id"])
        e["course"] = {"code": course["code"], "credits": course["credits"]} if course else None
    return {
        "student": results["student"][0],
        "enrollments": rows,
        "pending_payments": results["payments"][0],
        "unavailable": [name for name, (_, responded) in results.items() if not responded],
    }


# ---- Internos: los usa Teacher (patrón Branch); el Gateway NO los expone ----

@app.get("/internal/enrollments/counts")
def internal_counts(status: str = "CONFIRMADA"):
    with get_db(DB) as c:
        rows = c.execute(
            "SELECT course_id, COUNT(*) AS n FROM enrollments WHERE status = ? GROUP BY course_id", (status,)
        ).fetchall()
    return {str(r["course_id"]): r["n"] for r in rows}


@app.get("/internal/enrollments")
def internal_list(course_id: Annotated[Optional[int], Query(ge=-MAX_ID, le=MAX_ID)] = None,
                  status: Optional[str] = None):
    query, args, conditions = "SELECT id, student_id, student_username, course_id, status FROM enrollments", [], []
    if course_id is not None:
        conditions.append("course_id = ?")
        args.append(course_id)
    if status:
        conditions.append("status = ?")
        args.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    with get_db(DB) as c:
        rows = c.execute(query + " ORDER BY id LIMIT 500", args).fetchall()
    return [dict(r) for r in rows]


@app.get("/internal/enrollments/{enrollment_id}")
def internal_get(enrollment_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)]):
    with get_db(DB) as c:
        row = c.execute(
            "SELECT id, student_id, student_username, course_id, status FROM enrollments WHERE id = ?",
            (enrollment_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "La matrícula no existe")
    return dict(row)


@app.get("/enrollments/{enrollment_id}")
def get_enrollment(enrollment_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)], x_user_id: str = Header(...), x_user_role: str = Header(...)):
    with get_db(DB) as c:
        row = c.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not row or (x_user_role != "admin" and row["student_id"] != x_user_id):
        raise HTTPException(404, "La matrícula no existe")
    return dict(row)
