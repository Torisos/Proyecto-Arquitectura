"""Teacher Service: perfil del docente, cursos asignados y registro de notas.

Patrón Branch: para saber quiénes están matriculados, Teacher consulta a Enrollment
de forma síncrona (protegido por Circuit Breaker). Teacher guarda una copia del nombre
del curso, así no depende de Course.
"""
import logging
import os
import time
from contextlib import asynccontextmanager

from typing import Annotated

import httpx
from fastapi import FastAPI, Header, HTTPException, Path
from pydantic import BaseModel, Field

from common.db import get_db
from common.errors import install_error_handlers
from common.limits import MAX_ID
from common.resilience import CircuitBreaker, CircuitOpenError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("teacher")

DB = os.getenv("DB_PATH", "/data/teacher.db")
ENROLLMENT_URL = os.getenv("ENROLLMENT_URL", "http://enrollment:8000")

http = httpx.Client(timeout=httpx.Timeout(3.0, connect=1.5))
enrollment_breaker = CircuitBreaker(failure_threshold=3, reset_timeout=10)

SEED_TEACHERS = [("4", "Docente Uno", "Ingeniería de Software", "docente1@example.edu")]
SEED_ASSIGNMENTS = [("4", 1, "Arquitectura de Software"), ("4", 2, "Bases de Datos")]


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db(DB) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS teachers ("
            "user_id TEXT PRIMARY KEY, full_name TEXT NOT NULL, department TEXT NOT NULL, email TEXT NOT NULL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS assignments ("
            "teacher_id TEXT NOT NULL, course_id INTEGER NOT NULL, course_name TEXT NOT NULL, "
            "PRIMARY KEY (teacher_id, course_id))"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS grades ("
            "enrollment_id INTEGER PRIMARY KEY, teacher_id TEXT NOT NULL, course_id INTEGER NOT NULL, "
            "student_id TEXT NOT NULL, grade REAL NOT NULL, comment TEXT, updated_at REAL NOT NULL)"
        )
        if c.execute("SELECT COUNT(*) FROM teachers").fetchone()[0] == 0:
            c.executemany("INSERT INTO teachers VALUES (?, ?, ?, ?)", SEED_TEACHERS)
            c.executemany("INSERT INTO assignments VALUES (?, ?, ?)", SEED_ASSIGNMENTS)
            log.info("Docentes de ejemplo creados")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Teacher Service", lifespan=lifespan)
install_error_handlers(app)


def _enrollment_get(path, params=None):
    r = http.get(f"{ENROLLMENT_URL}{path}", params=params)
    if r.status_code >= 500:  # un 5xx cuenta como fallo para el Circuit Breaker
        raise httpx.HTTPError(f"Enrollment respondió {r.status_code}")
    return r


def call_enrollment(path, params=None):
    """Branch: llamada síncrona a Enrollment con Circuit Breaker."""
    return enrollment_breaker.call(_enrollment_get, path, params)


def _require_teacher(role):
    if role != "teacher":
        raise HTTPException(403, "Solo los docentes pueden usar este recurso")


def _require_assigned(teacher_id, course_id):
    with get_db(DB) as c:
        row = c.execute(
            "SELECT 1 FROM assignments WHERE teacher_id = ? AND course_id = ?", (teacher_id, course_id)
        ).fetchone()
    if not row:
        raise HTTPException(403, "No tienes asignado este curso")


class GradeRequest(BaseModel):
    enrollment_id: int = Field(ge=-MAX_ID, le=MAX_ID)
    grade: float = Field(ge=0, le=5)
    comment: str = Field(default="", max_length=300)


@app.get("/health")
def health():
    return {"service": "teacher", "status": "ok", "enrollment_breaker": enrollment_breaker.state}


@app.get("/teachers/me")
def my_profile(x_user_id: str = Header(...), x_user_role: str = Header(...)):
    _require_teacher(x_user_role)
    with get_db(DB) as c:
        row = c.execute("SELECT * FROM teachers WHERE user_id = ?", (x_user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No hay perfil de docente para este usuario")
    return dict(row)


@app.get("/teachers/me/courses")
def my_courses(x_user_id: str = Header(...), x_user_role: str = Header(...)):
    """Cursos asignados. Si Enrollment no responde, igual devuelve los cursos
    y marca los estudiantes como no disponibles (degradación controlada)."""
    _require_teacher(x_user_role)
    with get_db(DB) as c:
        rows = c.execute(
            "SELECT course_id, course_name FROM assignments WHERE teacher_id = ? ORDER BY course_id",
            (x_user_id,),
        ).fetchall()
    counts, available = {}, True
    try:
        r = call_enrollment("/internal/enrollments/counts", {"status": "CONFIRMADA"})
        counts = r.json() if r.status_code == 200 else {}
        available = r.status_code == 200
    except (CircuitOpenError, httpx.HTTPError):
        available = False
    return [
        {
            "course_id": r["course_id"],
            "course_name": r["course_name"],
            "enrolled_students": counts.get(str(r["course_id"]), 0) if available else None,
            "students_available": available,
        }
        for r in rows
    ]


@app.get("/teachers/me/courses/{course_id}/students")
def course_students(course_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)], x_user_id: str = Header(...), x_user_role: str = Header(...)):
    _require_teacher(x_user_role)
    _require_assigned(x_user_id, course_id)
    try:
        r = call_enrollment("/internal/enrollments", {"course_id": course_id, "status": "CONFIRMADA"})
    except (CircuitOpenError, httpx.HTTPError):
        raise HTTPException(503, "El servicio de matrículas no está disponible. Intenta más tarde.")
    if r.status_code != 200:
        raise HTTPException(502, "Respuesta inesperada del servicio de matrículas")
    return [
        {"enrollment_id": e["id"], "student_id": e["student_id"], "student_username": e.get("student_username")}
        for e in r.json()
    ]


@app.post("/teachers/me/courses/{course_id}/grades", status_code=201)
def register_grade(course_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)], body: GradeRequest,
                   x_user_id: str = Header(...), x_user_role: str = Header(...)):
    _require_teacher(x_user_role)
    _require_assigned(x_user_id, course_id)
    try:
        r = call_enrollment(f"/internal/enrollments/{body.enrollment_id}")
    except (CircuitOpenError, httpx.HTTPError):
        raise HTTPException(503, "No se puede verificar la matrícula ahora. Intenta más tarde.")
    if r.status_code == 404:
        raise HTTPException(404, "La matrícula no existe")
    enrollment = r.json()
    if enrollment["course_id"] != course_id or enrollment["status"] != "CONFIRMADA":
        raise HTTPException(409, "La matrícula no pertenece a este curso o no está confirmada")
    with get_db(DB) as c:
        c.execute(
            "INSERT INTO grades(enrollment_id, teacher_id, course_id, student_id, grade, comment, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(enrollment_id) DO UPDATE SET grade=excluded.grade, comment=excluded.comment, "
            "updated_at=excluded.updated_at",
            (body.enrollment_id, x_user_id, course_id, enrollment["student_id"],
             body.grade, body.comment or "", time.time()),
        )
    return {"enrollment_id": body.enrollment_id, "student_id": enrollment["student_id"], "grade": body.grade}


@app.get("/teachers/me/courses/{course_id}/grades")
def list_grades(course_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)], x_user_id: str = Header(...), x_user_role: str = Header(...)):
    _require_teacher(x_user_role)
    _require_assigned(x_user_id, course_id)
    with get_db(DB) as c:
        rows = c.execute(
            "SELECT enrollment_id, student_id, grade, comment FROM grades "
            "WHERE teacher_id = ? AND course_id = ? ORDER BY enrollment_id",
            (x_user_id, course_id),
        ).fetchall()
    return [dict(r) for r in rows]
