"""Utilidades HTTP compartidas por los scripts de prueba (solo librería estándar)."""
import http.client
import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("API_BASE", "http://localhost:8000")


def request(method, path, token=None, body=None, raw=None, headers=None, timeout=10, base=None):
    """Devuelve (status, cuerpo, segundos). status es None si no hubo respuesta
    (servicio caído, conexión rechazada, tiempo agotado...)."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request((base or BASE) + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        payload, status = e.read(), e.code
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return None, str(getattr(e, "reason", e)), time.time() - start
    elapsed = time.time() - start
    text = payload.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    return status, parsed, elapsed


def login(username, password):
    status, body, _ = request("POST", "/auth/login", body={"username": username, "password": password})
    if status != 200:
        raise RuntimeError("No se pudo iniciar sesión como %s (estado %s: %s). "
                           "¿Está corriendo 'docker compose up'?" % (username, status, body))
    return body["access_token"]
