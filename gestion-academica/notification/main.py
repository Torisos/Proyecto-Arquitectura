"""Notification Service: solo escucha eventos. No lo llama nadie, así que nadie depende de él."""
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from common.db import get_db
from common.errors import install_error_handlers
from common.events import start_consumer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("notification")

DB = os.getenv("DB_PATH", "/data/notification.db")

TEMPLATES = {
    "matricula.creada": "Tu solicitud de matrícula en {course_name} fue registrada. Tienes un pago pendiente de ${amount}.",
    "pago.confirmado": "Recibimos tu pago de ${amount} para {course_name}.",
    "pago.fallido": "Tu pago para {course_name} no pudo procesarse. La matrícula será cancelada.",
    "matricula.confirmada": "¡Matrícula confirmada en {course_name}!",
    "matricula.cancelada": "Tu matrícula en {course_name} fue cancelada. Motivo: {reason}.",
}


class _Safe(dict):
    def __missing__(self, key):
        return "?"


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db(DB) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS notifications ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, event TEXT NOT NULL, "
            "message TEXT NOT NULL, created_at REAL NOT NULL)"
        )


def handle_event(routing_key: str, payload: dict):
    template = TEMPLATES.get(routing_key)
    user_id = payload.get("student_id")
    if not template or not user_id:
        return
    data = dict(payload)
    if isinstance(data.get("amount"), (int, float)):
        data["amount"] = format(data["amount"], ",.0f").replace(",", ".")  # 1.500.000
    message = template.format_map(_Safe(data))
    with get_db(DB) as c:
        c.execute(
            "INSERT INTO notifications(user_id, event, message, created_at) VALUES (?, ?, ?, ?)",
            (str(user_id), routing_key, message, time.time()),
        )
    log.info("[correo/push simulado] usuario %s: %s", user_id, message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_consumer("notification.q", ["matricula.*", "pago.*"], handle_event)
    yield


app = FastAPI(title="Notification Service", lifespan=lifespan)
install_error_handlers(app)


@app.get("/health")
def health():
    return {"service": "notification", "status": "ok"}


@app.get("/notifications")
def list_notifications(x_user_id: str = Header(...), x_user_role: str = Header(...)):
    with get_db(DB) as c:
        if x_user_role == "admin":
            rows = c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 200").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 100", (x_user_id,)
            ).fetchall()
    return [dict(r) for r in rows]
