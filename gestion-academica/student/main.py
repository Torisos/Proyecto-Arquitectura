"""Student Service: perfil e información académica del estudiante.

Los user_id coinciden con los ids que Auth asigna al sembrar los usuarios de ejemplo
(admin=1, estudiante1=2, estudiante2=3, docente1=4).
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from common.db import get_db
from common.errors import install_error_handlers

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("student")

DB = os.getenv("DB_PATH", "/data/student.db")

SEED_STUDENTS = [
    ("2", "Estudiante Uno", "Ingeniería de Sistemas", 5, "estudiante1@example.edu"),
    ("3", "Estudiante Dos", "Ingeniería de Sistemas", 3, "estudiante2@example.edu"),
]


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db(DB) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS students ("
            "user_id TEXT PRIMARY KEY, full_name TEXT NOT NULL, program TEXT NOT NULL, "
            "semester INTEGER NOT NULL, email TEXT NOT NULL)"
        )
        if c.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0:
            c.executemany("INSERT INTO students VALUES (?, ?, ?, ?, ?)", SEED_STUDENTS)
            log.info("Estudiantes de ejemplo creados")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Student Service", lifespan=lifespan)
install_error_handlers(app)


def _get_student(user_id: str):
    with get_db(DB) as c:
        row = c.execute("SELECT * FROM students WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


@app.get("/health")
def health():
    return {"service": "student", "status": "ok"}


@app.get("/students/me")
def my_profile(x_user_id: str = Header(...), x_user_role: str = Header(...)):
    if x_user_role != "student":
        raise HTTPException(403, "Solo los estudiantes tienen perfil de estudiante")
    student = _get_student(x_user_id)
    if not student:
        raise HTTPException(404, "No hay perfil de estudiante para este usuario")
    return student


@app.get("/students")
def list_students(x_user_role: str = Header(...)):
    if x_user_role != "admin":
        raise HTTPException(403, "Solo el área administrativa puede listar estudiantes")
    with get_db(DB) as c:
        rows = c.execute("SELECT * FROM students ORDER BY user_id").fetchall()
    return [dict(r) for r in rows]


# ---- Interno: lo usa el Aggregator de Enrollment; el Gateway NO lo expone ----

@app.get("/internal/students/{user_id}")
def internal_student(user_id: str):
    student = _get_student(user_id)
    if not student:
        raise HTTPException(404, "El estudiante no existe")
    return student
