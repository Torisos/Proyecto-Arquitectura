"""API Gateway: punto único de entrada. Valida el JWT, limita peticiones y enruta a cada servicio."""
import collections
import logging
import os
import time
from urllib.parse import quote

import httpx
import jwt
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

# El Gateway valida el token localmente con el secreto compartido: si Auth se cae,
# los tokens vigentes siguen funcionando.
JWT_SECRET = os.getenv("JWT_SECRET", "cambia-este-secreto")
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "100"))
RATE_WINDOW = float(os.getenv("RATE_WINDOW_SECONDS", "10"))

UPSTREAMS = {
    "auth": os.getenv("AUTH_URL", "http://auth:8000"),
    "courses": os.getenv("COURSE_URL", "http://course:8000"),
    "enrollments": os.getenv("ENROLLMENT_URL", "http://enrollment:8000"),
    "payments": os.getenv("PAYMENT_URL", "http://payment:8000"),
    "notifications": os.getenv("NOTIFICATION_URL", "http://notification:8000"),
    "students": os.getenv("STUDENT_URL", "http://student:8000"),
    "teachers": os.getenv("TEACHER_URL", "http://teacher:8000"),
}
# Cualquier ruta que no empiece por estos prefijos (por ejemplo /internal/...) devuelve 404.

SKIP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
    "te", "trailers", "proxy-authorization",
    # el cliente no puede falsificar su identidad: estos los pone el Gateway
    "x-user-id", "x-user-role", "x-user-name",
}
PASS_RESPONSE_HEADERS = {"content-type", "x-cache"}
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

app = FastAPI(title="API Gateway")
client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
_hits = collections.defaultdict(collections.deque)


def _too_many_requests(ip: str) -> bool:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


async def _forward(prefix: str, request: Request):
    ip = request.client.host if request.client else "desconocido"
    if _too_many_requests(ip):
        return _error(429, "Demasiadas peticiones. Intenta de nuevo en unos segundos.")

    base = UPSTREAMS.get(prefix)
    if base is None:
        return _error(404, "Ruta no encontrada")

    path = request.url.path
    # Se reenvía la ruta ya codificada: así '%3F', '%23' o '%00' no cambian el significado de la URL
    # ni la rompen, y el servicio recibe exactamente la ruta que el Gateway autorizó.
    upstream_path = quote(path, safe="/")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in SKIP_REQUEST_HEADERS}

    is_public = request.method == "POST" and path == "/auth/login"
    if not is_public:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _error(401, "Falta el token (Authorization: Bearer <token>)")
        try:
            claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return _error(401, "Token vencido")
        except jwt.InvalidTokenError:
            return _error(401, "Token inválido")
        headers["x-user-id"] = str(claims.get("sub", ""))
        headers["x-user-role"] = str(claims.get("role", ""))
        headers["x-user-name"] = str(claims.get("username", ""))

    try:
        upstream = await client.request(
            request.method,
            f"{base}{upstream_path}",
            params=request.query_params.multi_items(),
            content=await request.body(),
            headers=headers,
        )
    except httpx.InvalidURL:
        return _error(400, "La ruta contiene caracteres no válidos")
    except httpx.ConnectTimeout as exc:
        # no se pudo ni conectar: el servicio está caído (no es lo mismo que un servicio lento)
        log.warning("'%s' no responde (%s)", prefix, type(exc).__name__)
        return _error(503, f"El servicio '{prefix}' no está disponible")
    except httpx.TimeoutException as exc:
        log.warning("'%s' demasiado lento (%s)", prefix, type(exc).__name__)
        return _error(504, f"El servicio '{prefix}' tardó demasiado en responder")
    except httpx.TransportError as exc:
        log.warning("'%s' no disponible (%s)", prefix, type(exc).__name__)
        return _error(503, f"El servicio '{prefix}' no está disponible")
    except httpx.HTTPError as exc:
        log.warning("Error hablando con '%s' (%s)", prefix, type(exc).__name__)
        return _error(502, f"Error al comunicarse con el servicio '{prefix}'")

    out = {k: v for k, v in upstream.headers.items() if k.lower() in PASS_RESPONSE_HEADERS}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out)


@app.get("/health")
async def health():
    return {"service": "gateway", "status": "ok"}


@app.api_route("/{prefix}", methods=METHODS)
async def root_route(prefix: str, request: Request):
    return await _forward(prefix, request)


@app.api_route("/{prefix}/{rest:path}", methods=METHODS)
async def nested_route(prefix: str, rest: str, request: Request):
    return await _forward(prefix, request)
