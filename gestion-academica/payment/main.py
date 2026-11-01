"""Payment Service: pagos académicos. Reacciona a 'matricula.creada' creando el cobro pendiente."""
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import FastAPI, Header, HTTPException, Path
from pydantic import BaseModel

from common.db import get_db
from common.errors import install_error_handlers
from common.limits import MAX_ID
from common.events import Outbox, start_consumer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("payment")

DB = os.getenv("DB_PATH", "/data/payment.db")
outbox = Outbox(DB)


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db(DB) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS payments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, enrollment_id INTEGER UNIQUE NOT NULL, "
            "student_id TEXT NOT NULL, course_name TEXT, amount REAL NOT NULL, status TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
    outbox.init()


def handle_event(routing_key: str, payload: dict):
    enrollment_id = payload.get("enrollment_id")
    if enrollment_id is None:
        return
    now = time.time()
    with get_db(DB) as c:
        if routing_key == "matricula.creada":
            # INSERT OR IGNORE: si el evento llega repetido no se duplica el cobro
            c.execute(
                "INSERT OR IGNORE INTO payments(enrollment_id, student_id, course_name, amount, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'PENDIENTE', ?, ?)",
                (enrollment_id, str(payload["student_id"]), payload.get("course_name"),
                 float(payload["amount"]), now, now),
            )
            log.info("Cobro pendiente creado para la matrícula %s", enrollment_id)
        elif routing_key == "matricula.cancelada":
            c.execute(
                "UPDATE payments SET status='CANCELADO', updated_at=? WHERE enrollment_id=? AND status='PENDIENTE'",
                (now, enrollment_id),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_consumer("payment.q", ["matricula.creada", "matricula.cancelada"], handle_event)
    outbox.start_flusher()
    yield


app = FastAPI(title="Payment Service", lifespan=lifespan)
install_error_handlers(app)


class PayRequest(BaseModel):
    simulate_failure: bool = False  # para probar la compensación de la Saga


@app.get("/health")
def health():
    return {"service": "payment", "status": "ok"}


@app.get("/payments")
def list_payments(status: Optional[str] = None, x_user_id: str = Header(...), x_user_role: str = Header(...)):
    if x_user_role not in ("student", "admin"):
        raise HTTPException(403, "No tienes permiso para ver pagos")
    query, args = "SELECT * FROM payments", []
    conditions = []
    if x_user_role == "student":
        conditions.append("student_id = ?")
        args.append(x_user_id)
    if status:
        conditions.append("status = ?")
        args.append(status.upper())
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT 200"
    with get_db(DB) as c:
        rows = c.execute(query, args).fetchall()
    return [dict(r) for r in rows]


# ---- Interno: lo usa el Aggregator de Enrollment; el Gateway NO lo expone ----

@app.get("/internal/payments")
def internal_payments(student_id: str, status: Optional[str] = None):
    query, args = "SELECT * FROM payments WHERE student_id = ?", [student_id]
    if status:
        query += " AND status = ?"
        args.append(status.upper())
    with get_db(DB) as c:
        rows = c.execute(query + " ORDER BY id DESC LIMIT 200", args).fetchall()
    return [dict(r) for r in rows]


@app.post("/payments/{payment_id}/pay")
def pay(payment_id: Annotated[int, Path(ge=-MAX_ID, le=MAX_ID)], body: Optional[PayRequest] = None,
        x_user_id: str = Header(...), x_user_role: str = Header(...)):
    if x_user_role != "student":
        raise HTTPException(403, "Solo los estudiantes pagan sus matrículas")
    fail = bool(body and body.simulate_failure)
    with get_db(DB) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not row or row["student_id"] != x_user_id:
            raise HTTPException(404, "El pago no existe")
        if row["status"] != "PENDIENTE":
            raise HTTPException(409, f"El pago ya está en estado {row['status']}")
        new_status = "FALLIDO" if fail else "PAGADO"
        c.execute("UPDATE payments SET status=?, updated_at=? WHERE id=?", (new_status, time.time(), payment_id))
        Outbox.add(c, "pago.fallido" if fail else "pago.confirmado", {
            "enrollment_id": row["enrollment_id"], "payment_id": payment_id,
            "student_id": row["student_id"], "course_name": row["course_name"], "amount": row["amount"],
        })
    outbox.flush_async()
    return {"id": payment_id, "status": new_status}
