"""Course Service: oferta académica y cupos. Incluye una Cache en memoria para la lista de cursos."""
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Path, Response
from pydantic import BaseModel, Field

from common.db import get_db
from common.errors import install_error_handlers
from common.limits import MAX_ID

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("course")

DB = os.getenv("DB_PATH", "/data/course.db")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "30"))

_cache = {"data": None, "at": 0.0}
_cache_lock = threading.Lock()

SEED_COURSES = [
    ("ARQ101", "Arquitectura de Software", 3, 1500000, 30),
    ("BD201", "Bases de Datos", 3, 1400000, 25),
    ("RED301", "Redes de Computadores", 4, 1800000, 2),  # capacidad 2 para probar "sin cupos"
]


def invalidate_cache():
    with _cache_lock:
        _cache["data"] = None


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db(DB) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS courses ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, "
            "credits INTEGER NOT NULL, price REAL NOT NULL, capacity INTEGER NOT NULL, "
            "enrolled INTEGER NOT NULL DEFAULT 0)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS reservations ("
            "enrollment_id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL)"
        )
        if c.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO courses(code, name, credits, price, capacity) VALUES (?, ?, ?, ?, ?)",
                SEED_COURSES,
            )
            log.info("Cursos de ejemplo creados")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Course Service", lifespan=lifespan)
install_error_handlers(app)


def to_course(r):
    return {
        "id": r["id"],
        "code": r["code"],
        "name": r["name"],
        "credits": r["credits"],
        "price": r["price"],
        "capacity": r["capacity"],
        "available": r["capacity"] - r["enrolled"],
    }


class CourseCreate(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    credits: int = Field(ge=1, le=10)
    price: float = Field(ge=0, le=100_000_000)
    capacity: int = Field(ge=1, le=500)


class ReservationRequest(BaseModel):
    enrollment_id: int = Field(ge=-MAX_ID, le=MAX_ID)


@app.get("/health")
def health():
    return {"service": "course", "status": "ok"}


@app.get("/courses")
def list_courses(response: Response):
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
            response.headers["X-Cache"] = "HIT"
            return _cache["data"]
    with get_db(DB) as c:
        rows = c.execute("SELECT * FROM courses ORDER BY id").fetchall()
    data = [to_course(r) for r in rows]
    with _cache_lock:
        _cache["data"] = data
        _cache["at"] = now
    response.headers["X-Cache"] = "MISS"
    return data


@app.get("/courses/{course_id}")
def get_course(course_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)]):
    with get_db(DB) as c:
        row = c.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not row:
        raise HTTPException(404, "El curso no existe")
    return to_course(row)


@app.post("/courses", status_code=201)
def create_course(body: CourseCreate, x_user_role: str = Header(...)):
    if x_user_role != "admin":
        raise HTTPException(403, "Solo el área administrativa puede crear cursos")
    with get_db(DB) as c:
        if c.execute("SELECT 1 FROM courses WHERE code = ?", (body.code,)).fetchone():
            raise HTTPException(409, "Ya existe un curso con ese código")
        cur = c.execute(
            "INSERT INTO courses(code, name, credits, price, capacity) VALUES (?, ?, ?, ?, ?)",
            (body.code, body.name, body.credits, body.price, body.capacity),
        )
        row = c.execute("SELECT * FROM courses WHERE id = ?", (cur.lastrowid,)).fetchone()
    invalidate_cache()
    return to_course(row)


# ---- Endpoints internos: los usa Enrollment, el Gateway NO los expone ----

@app.post("/internal/courses/{course_id}/reserve")
def reserve(course_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)], body: ReservationRequest):
    """Reserva un cupo. Es idempotente: repetir la misma matrícula no gasta otro cupo."""
    with get_db(DB) as c:
        c.execute("BEGIN IMMEDIATE")
        course = c.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
        if not course:
            raise HTTPException(404, "El curso no existe")
        already = c.execute(
            "SELECT 1 FROM reservations WHERE enrollment_id = ?", (body.enrollment_id,)
        ).fetchone()
        if not already:
            if course["enrolled"] >= course["capacity"]:
                raise HTTPException(409, "Sin cupos disponibles")
            c.execute(
                "INSERT INTO reservations(enrollment_id, course_id) VALUES (?, ?)",
                (body.enrollment_id, course_id),
            )
            c.execute("UPDATE courses SET enrolled = enrolled + 1 WHERE id = ?", (course_id,))
    invalidate_cache()
    return {"course_id": course_id, "name": course["name"], "price": course["price"]}


@app.post("/internal/courses/{course_id}/release")
def release(course_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)], body: ReservationRequest):
    """Libera el cupo (compensación de la Saga). También es idempotente."""
    released = False
    with get_db(DB) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT 1 FROM reservations WHERE enrollment_id = ? AND course_id = ?",
            (body.enrollment_id, course_id),
        ).fetchone()
        if row:
            c.execute("DELETE FROM reservations WHERE enrollment_id = ?", (body.enrollment_id,))
            c.execute("UPDATE courses SET enrolled = MAX(enrolled - 1, 0) WHERE id = ?", (course_id,))
            released = True
    invalidate_cache()
    return {"released": released}
