"""Auth Service: login y emisión de tokens JWT (SSO). Es el único que conoce las credenciales."""
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

import jwt
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from common.db import get_db
from common.errors import install_error_handlers

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auth")

DB = os.getenv("DB_PATH", "/data/auth.db")
JWT_SECRET = os.getenv("JWT_SECRET", "cambia-este-secreto")
TOKEN_TTL = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))

SEED_USERS = [
    ("admin", "admin123", "admin"),
    ("estudiante1", "est123", "student"),
    ("estudiante2", "est123", "student"),
    ("docente1", "doc123", "teacher"),
]


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 100_000).hex()


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db(DB) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
            "password_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL)"
        )
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for username, password, role in SEED_USERS:
                salt = secrets.token_hex(16)
                c.execute(
                    "INSERT INTO users(username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
                    (username, hash_password(password, salt), salt, role),
                )
            log.info("Usuarios de ejemplo creados")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Auth Service", lifespan=lifespan)
install_error_handlers(app)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@app.get("/health")
def health():
    return {"service": "auth", "status": "ok"}


@app.post("/auth/login")
def login(body: LoginRequest):
    with get_db(DB) as c:
        user = c.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    # Siempre se calcula el hash para que el tiempo de respuesta no revele si el usuario existe.
    salt = user["salt"] if user else "00" * 16
    candidate = hash_password(body.password, salt)
    if not user or not hmac.compare_digest(candidate, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(user["id"]),
            "username": user["username"],
            "role": user["role"],
            "iat": now,
            "exp": now + TOKEN_TTL,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_TTL, "role": user["role"]}


@app.get("/auth/me")
def me(x_user_id: str = Header(...), x_user_role: str = Header(...), x_user_name: str = Header("")):
    """Devuelve quién es el usuario según el token que ya validó el Gateway."""
    return {"id": x_user_id, "role": x_user_role, "username": x_user_name}
