"""Monkey test / Fuzzing contra el API Gateway (solo librería estándar).

Envía entradas aleatorias, inválidas o maliciosas y comprueba que el sistema responda
con un error de cliente controlado (4xx), nunca con un error del servidor (5xx), y que
no acepte lo que debería rechazar. Al final lanza una ráfaga de peticiones para ver
el rate limiting.

Uso:   python scripts/fuzz.py            (el sistema debe estar corriendo)
Salida: tabla en consola y un archivo en resultados/fuzz_<fecha>.md
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import common_http as http

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACING = 0.12      # segundos entre peticiones, para no activar el rate limit con el propio fuzzing
SLOW_SECONDS = 3.0


# ---------- utilidades para fabricar tokens falsos ----------
def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def forge(header, payload, secret=None):
    h, p = b64(json.dumps(header).encode()), b64(json.dumps(payload).encode())
    signature = b64(hmac.new(secret.encode(), ("%s.%s" % (h, p)).encode(), hashlib.sha256).digest()) if secret else ""
    return "%s.%s.%s" % (h, p, signature)


# ---------- casos de prueba ----------
def build_cases(tok):
    student, teacher, admin = tok["student"], tok["teacher"], tok["admin"]
    cases = []

    def add(cat, desc, method, path, token=None, body=None, raw=None, headers=None, ok=(422,)):
        cases.append({"cat": cat, "desc": desc, "method": method, "path": path, "token": token,
                      "body": body, "raw": raw, "headers": headers, "ok": ok})

    big = "A" * 10000
    claims = {"sub": "1", "username": "admin", "role": "admin", "exp": int(time.time()) + 3600}

    # --- Auth: login ---
    login_ok = (400, 401, 415, 422)
    path = "/auth/login"
    add("Auth", "login con cuerpo vacío {}", "POST", path, body={}, ok=login_ok)
    add("Auth", "usuario vacío", "POST", path, body={"username": "", "password": "x"}, ok=login_ok)
    add("Auth", "contraseña vacía", "POST", path, body={"username": "admin", "password": ""}, ok=login_ok)
    add("Auth", "usuario de 10.000 caracteres", "POST", path, body={"username": big, "password": "x"}, ok=login_ok)
    add("Auth", "contraseña de 10.000 caracteres", "POST", path, body={"username": "admin", "password": big}, ok=login_ok)
    add("Auth", "inyección SQL (OR '1'='1)", "POST", path, body={"username": "admin' OR '1'='1", "password": "x"}, ok=login_ok)
    add("Auth", "inyección SQL (DROP TABLE)", "POST", path, body={"username": "'; DROP TABLE users;--", "password": "x"}, ok=login_ok)
    add("Auth", "emoji y unicode", "POST", path, body={"username": "😀ñ中", "password": "😀"}, ok=login_ok)
    add("Auth", "byte nulo en el usuario", "POST", path, body={"username": "adm\u0000in", "password": "x"}, ok=login_ok)
    add("Auth", "tipos incorrectos (números)", "POST", path, body={"username": 123, "password": 456}, ok=login_ok)
    add("Auth", "valores null", "POST", path, body={"username": None, "password": None}, ok=login_ok)
    add("Auth", "listas en lugar de textos", "POST", path, body={"username": ["a"], "password": ["b"]}, ok=login_ok)
    add("Auth", "JSON malformado", "POST", path, raw=b'{"username": "a",', ok=login_ok)
    add("Auth", "cuerpo que no es JSON", "POST", path, raw=b"hola mundo", ok=login_ok)
    add("Auth", "sin cuerpo", "POST", path, raw=b"", ok=login_ok)
    add("Auth", "Content-Type text/plain", "POST", path, raw=b'{"username":"admin","password":"x"}',
        headers={"Content-Type": "text/plain"}, ok=login_ok)
    add("Auth", "cuerpo de 2 MB", "POST", path, body={"username": "A" * 2000000, "password": "x"}, ok=login_ok + (413,))

    # --- Gateway: tokens y cabeceras ---
    gw = "/courses"
    h, p, _sig = student.split(".")
    tampered = json.loads(b64d(p))
    tampered["role"] = "admin"
    tampered_token = "%s.%s.%s" % (h, b64(json.dumps(tampered).encode()), student.split(".")[2])
    add("Gateway", "sin cabecera Authorization", "GET", gw, ok=(401,))
    add("Gateway", "'Bearer' sin token", "GET", gw, headers={"Authorization": "Bearer "}, ok=(401,))
    add("Gateway", "token basura", "GET", gw, token="esto.no.es.un.token", ok=(401,))
    add("Gateway", "esquema Basic en vez de Bearer", "GET", gw, headers={"Authorization": "Basic YWRtaW46YWRtaW4="}, ok=(401,))
    add("Gateway", "token con la firma alterada", "GET", gw, token=student[:-4] + ("AAAA" if not student.endswith("AAAA") else "BBBB"), ok=(401,))
    add("Gateway", "payload alterado (rol admin) con la firma original", "GET", gw, token=tampered_token, ok=(401,))
    add("Gateway", "token con alg=none y rol admin", "GET", gw, token=forge({"alg": "none", "typ": "JWT"}, claims), ok=(401,))
    add("Gateway", "token firmado con otra clave (rol admin)", "GET", gw, token=forge({"alg": "HS256", "typ": "JWT"}, claims, "otra-clave"), ok=(401,))
    add("Gateway", "token de 20.000 caracteres", "GET", gw, token="A" * 20000, ok=(401, 431))
    add("Gateway", "falsificar X-User-Role: admin con token de estudiante", "POST", "/courses", token=student,
        body={"code": "FZ1", "name": "x", "credits": 1, "price": 1, "capacity": 1},
        headers={"X-User-Role": "admin", "X-User-Id": "1"}, ok=(403,))
    add("Gateway", "ruta interna /internal/...", "POST", "/internal/courses/1/reserve", token=student, body={"enrollment_id": 1}, ok=(404,))
    add("Gateway", "ruta inexistente", "GET", "/no-existe", token=student, ok=(404,))
    add("Gateway", "path traversal", "GET", "/courses/..%2f..%2fetc%2fpasswd", token=student, ok=(404, 422))

    # --- Course ---
    huge = "99999999999999999999999999"
    add("Course", "id de curso no numérico", "GET", "/courses/abc", token=student, ok=(422,))
    add("Course", "id de curso = 0", "GET", "/courses/0", token=student, ok=(404,))
    add("Course", "id de curso negativo", "GET", "/courses/-1", token=student, ok=(404,))
    add("Course", "id de curso decimal (1.5)", "GET", "/courses/1.5", token=student, ok=(422,))
    add("Course", "id de curso enorme (26 dígitos)", "GET", "/courses/" + huge, token=student, ok=(404, 422))
    add("Course", "id con emoji", "GET", "/courses/%F0%9F%98%80", token=student, ok=(422,))
    add("Course", "id con byte nulo", "GET", "/courses/%00", token=student, ok=(422,))
    add("Course", "id negativo enorme (-26 dígitos)", "GET", "/courses/-" + huge, token=student, ok=(404, 422))
    add("Course", "id con espacio codificado (%20)", "GET", "/courses/%20", token=student, ok=(422,))
    add("Course", "'?' codificado dentro de la ruta (%3F)", "GET", "/courses/1%3Fx=1", token=student, ok=(422,))
    add("Course", "'#' codificado dentro de la ruta (%23)", "GET", "/courses/1%23x", token=student, ok=(422,))
    add("Course", "crear curso con precio negativo", "POST", "/courses", token=admin,
        body={"code": "FZ2", "name": "x", "credits": 1, "price": -5, "capacity": 1})
    add("Course", "crear curso con capacidad 0", "POST", "/courses", token=admin,
        body={"code": "FZ3", "name": "x", "credits": 1, "price": 1, "capacity": 0})
    add("Course", "crear curso con capacidad de un billón", "POST", "/courses", token=admin,
        body={"code": "FZ4", "name": "x", "credits": 1, "price": 1, "capacity": 10 ** 12})
    add("Course", "crear curso con créditos no numéricos", "POST", "/courses", token=admin,
        body={"code": "FZ5", "name": "x", "credits": "abc", "price": 1, "capacity": 1})
    add("Course", "crear curso con nombre de 100.000 caracteres", "POST", "/courses", token=admin,
        body={"code": "FZ6", "name": "N" * 100000, "credits": 1, "price": 1, "capacity": 1})
    add("Course", "crear curso con precio Infinity", "POST", "/courses", token=admin,
        raw=b'{"code":"FZ7","name":"x","credits":1,"price":Infinity,"capacity":1}')

    # --- Enrollment ---
    enr = "/enrollments"
    enr_ok = (404, 422)
    add("Enrollment", "matrícula con cuerpo vacío {}", "POST", enr, token=student, body={}, ok=enr_ok)
    add("Enrollment", "course_id no numérico", "POST", enr, token=student, body={"course_id": "abc"}, ok=enr_ok)
    add("Enrollment", "course_id null", "POST", enr, token=student, body={"course_id": None}, ok=enr_ok)
    add("Enrollment", "course_id = -1", "POST", enr, token=student, body={"course_id": -1}, ok=enr_ok)
    add("Enrollment", "course_id = 0", "POST", enr, token=student, body={"course_id": 0}, ok=enr_ok)
    add("Enrollment", "course_id inexistente (999999)", "POST", enr, token=student, body={"course_id": 999999}, ok=enr_ok)
    add("Enrollment", "course_id decimal (1.5)", "POST", enr, token=student, body={"course_id": 1.5}, ok=enr_ok)
    add("Enrollment", "course_id es una lista", "POST", enr, token=student, body={"course_id": [1]}, ok=enr_ok)
    add("Enrollment", "course_id es un objeto", "POST", enr, token=student, body={"course_id": {"a": 1}}, ok=enr_ok)
    add("Enrollment", "course_id enorme (10^30)", "POST", enr, token=student, body={"course_id": 10 ** 30}, ok=enr_ok)
    add("Enrollment", "course_id = NaN", "POST", enr, token=student, raw=b'{"course_id": NaN}', ok=enr_ok)
    add("Enrollment", "course_id = Infinity", "POST", enr, token=student, raw=b'{"course_id": Infinity}', ok=enr_ok)
    add("Enrollment", "course_id negativo enorme (-10^30)", "POST", enr, token=student, body={"course_id": -10 ** 30}, ok=enr_ok)
    add("Enrollment", "campos de más (student_id, role)", "POST", enr, token=student,
        body={"course_id": 999999, "student_id": "1", "role": "admin"}, ok=enr_ok)
    add("Enrollment", "JSON malformado", "POST", enr, token=student, raw=b'{"course_id": ', ok=enr_ok)
    add("Enrollment", "cuerpo es una lista []", "POST", enr, token=student, raw=b"[]", ok=enr_ok)
    add("Enrollment", "cuerpo es null", "POST", enr, token=student, raw=b"null", ok=enr_ok)
    add("Enrollment", "consultar matrícula 0", "GET", "/enrollments/0", token=student, ok=(404,))
    add("Enrollment", "consultar matrícula no numérica", "GET", "/enrollments/abc", token=student, ok=(422,))
    add("Enrollment", "consultar matrícula enorme (26 dígitos)", "GET", "/enrollments/" + huge, token=student, ok=(404, 422))

    # --- Payment ---
    add("Payment", "pagar un cobro inexistente", "POST", "/payments/999999/pay", token=student, body={}, ok=(404,))
    add("Payment", "pagar con id no numérico", "POST", "/payments/abc/pay", token=student, body={}, ok=(422,))
    add("Payment", "pagar con id negativo", "POST", "/payments/-5/pay", token=student, body={}, ok=(404,))
    add("Payment", "pagar con id enorme (26 dígitos)", "POST", "/payments/%s/pay" % huge, token=student, body={}, ok=(404, 422))
    add("Payment", "pagar con id negativo enorme (-26 dígitos)", "POST", "/payments/-%s/pay" % huge, token=student, body={}, ok=(404, 422))
    add("Payment", "simulate_failure = NaN", "POST", "/payments/999999/pay", token=student, raw=b'{"simulate_failure": NaN}', ok=(404, 422))
    add("Payment", "filtro status con byte nulo", "GET", "/payments?status=%00", token=student, ok=(200,))
    add("Payment", "simulate_failure con tipo inválido", "POST", "/payments/999999/pay", token=student,
        body={"simulate_failure": "quizás"}, ok=(404, 422))
    add("Payment", "filtro status con inyección SQL", "GET", "/payments?status=%27%3B%20DROP%20TABLE%20payments%3B--", token=student, ok=(200,))
    add("Payment", "filtro status de 10.000 caracteres", "GET", "/payments?status=" + big, token=student, ok=(200, 414, 431))
    add("Payment", "un docente consulta pagos", "GET", "/payments", token=teacher, ok=(403,))

    # --- Teacher / Student / Notification ---
    add("Teacher", "un estudiante usa rutas de docente", "GET", "/teachers/me/courses", token=student, ok=(403,))
    add("Teacher", "curso no asignado al docente", "GET", "/teachers/me/courses/3/students", token=teacher, ok=(403,))
    add("Teacher", "id de curso no numérico", "GET", "/teachers/me/courses/abc/students", token=teacher, ok=(422,))
    add("Teacher", "id de curso enorme (26 dígitos)", "GET", "/teachers/me/courses/%s/students" % huge, token=teacher, ok=(403, 404, 422))
    add("Teacher", "id de curso negativo enorme (-26 dígitos)", "GET", "/teachers/me/courses/-%s/students" % huge, token=teacher, ok=(403, 404, 422))
    grades = "/teachers/me/courses/1/grades"
    add("Teacher", "nota fuera de rango (99)", "POST", grades, token=teacher, body={"enrollment_id": 1, "grade": 99})
    add("Teacher", "nota negativa", "POST", grades, token=teacher, body={"enrollment_id": 1, "grade": -1})
    add("Teacher", "nota NaN", "POST", grades, token=teacher, raw=b'{"enrollment_id":1,"grade":NaN}')
    add("Teacher", "enrollment_id no numérico", "POST", grades, token=teacher, body={"enrollment_id": "x", "grade": 3})
    add("Teacher", "falta la nota", "POST", grades, token=teacher, body={"enrollment_id": 1})
    add("Teacher", "comentario de 10.000 caracteres", "POST", grades, token=teacher,
        body={"enrollment_id": 1, "grade": 3, "comment": big})
    add("Student", "un estudiante lista a todos los estudiantes", "GET", "/students", token=student, ok=(403,))
    add("Notification", "parámetros de consulta basura", "GET", "/notifications?limit=abc&offset=-1", token=student, ok=(200,))
    return cases


def judge(status, ok, elapsed):
    if status is None:
        return "FALLA", "sin respuesta del servidor"
    if status >= 500:
        return "FALLA", "error del servidor (%d)" % status
    if status in ok:
        return ("LENTO", "tardó %.1f s" % elapsed) if elapsed > SLOW_SECONDS else ("OK", "")
    return "FALLA", "respuesta inesperada (%d)" % status


def run_case(case):
    for attempt in range(2):
        status, body, elapsed = http.request(case["method"], case["path"], token=case["token"], body=case["body"],
                                             raw=case["raw"], headers=case["headers"], timeout=15)
        if status != 429:
            break
        time.sleep(3)  # rate limit del propio fuzzing: esperar y repetir una vez
    verdict, note = judge(status, case["ok"], elapsed)
    if verdict == "FALLA" and status is not None and status >= 500 and isinstance(body, (dict, str)):
        note += " - " + str(body)[:90].replace("\n", " ")
    return {"cat": case["cat"], "desc": case["desc"], "esperado": "/".join(str(c) for c in case["ok"]),
            "status": status, "elapsed": elapsed, "verdict": verdict, "note": note}


def flood(token, total=300, workers=30):
    """Ráfaga de peticiones: debe activar el rate limit sin generar errores 5xx."""
    def one(_):
        status, _b, _e = http.request("GET", "/courses", token=token, timeout=15)
        return status
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts = Counter(pool.map(one, range(total)))
    secs = time.time() - start
    time.sleep(11)  # la ventana del limitador es de 10 s
    after, _b, _e = http.request("GET", "/courses", token=token)
    server_errors = sum(n for s, n in counts.items() if s is None or s >= 500)
    limited = counts.get(429, 0)
    ok = limited > 0 and server_errors == 0 and after == 200
    detail = "%d peticiones en %.1f s: %s; tras 11 s de pausa -> %s" % (
        total, secs, ", ".join("%s x%d" % (s, n) for s, n in sorted(counts.items(), key=lambda kv: str(kv[0]))), after)
    note = ""
    if limited == 0 and server_errors == 0:
        note = "no se activó el rate limit (¿equipo muy lento o RATE_LIMIT alto?)"
    return {"cat": "Carga", "desc": "ráfaga de %d peticiones concurrentes" % total, "esperado": "429 sin 5xx y recuperación",
            "status": detail, "elapsed": secs, "verdict": "OK" if ok else "FALLA", "note": note}


def write_report(results, started):
    os.makedirs(os.path.join(ROOT, "resultados"), exist_ok=True)
    path = os.path.join(ROOT, "resultados", "fuzz_%s.md" % started.strftime("%Y%m%d_%H%M%S"))
    counts = Counter(r["verdict"] for r in results)
    lines = ["# Monkey test / Fuzzing", "", "Fecha: %s" % started.strftime("%Y-%m-%d %H:%M"), "",
             "Total: %d casos. OK: %d. Lentos: %d. Fallas: %d." % (len(results), counts["OK"], counts["LENTO"], counts["FALLA"]), "",
             "| Categoría | Entrada | Esperado | Observado | Tiempo | Resultado |", "|---|---|---|---|---|---|"]
    for r in results:
        obs = str(r["status"]) + ((" — " + r["note"]) if r["note"] else "")
        lines.append("| %s | %s | %s | %s | %.0f ms | %s |" % (
            r["cat"], r["desc"].replace("|", "/"), r["esperado"], obs.replace("|", "/"), r["elapsed"] * 1000, r["verdict"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description="Fuzzing contra el API Gateway")
    parser.add_argument("--base", default=http.BASE, help="URL del Gateway (por defecto %(default)s)")
    parser.add_argument("--sin-rafaga", action="store_true", help="omite la ráfaga de carga final")
    args = parser.parse_args()
    http.BASE = args.base

    try:
        tokens = {"student": http.login("estudiante1", "est123"),
                  "teacher": http.login("docente1", "doc123"),
                  "admin": http.login("admin", "admin123")}
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)

    started = datetime.now()
    cases = build_cases(tokens)
    print("Ejecutando %d casos contra %s ...\n" % (len(cases), http.BASE))
    results = []
    for case in cases:
        time.sleep(PACING)
        r = run_case(case)
        results.append(r)
        print("[%-5s] %-12s %-58s -> %s (%.0f ms) %s" % (r["verdict"], r["cat"], r["desc"][:58], r["status"],
                                                        r["elapsed"] * 1000, r["note"]))
    if not args.sin_rafaga:
        print("\nLanzando ráfaga de carga (tarda ~15 s)...")
        r = flood(tokens["student"])
        results.append(r)
        print("[%-5s] %s: %s %s" % (r["verdict"], r["desc"], r["status"], r["note"]))

    counts = Counter(r["verdict"] for r in results)
    print("\n=== Resumen: %d casos | OK %d | LENTO %d | FALLA %d" % (len(results), counts["OK"], counts["LENTO"], counts["FALLA"]))
    failures = [r for r in results if r["verdict"] == "FALLA"]
    for r in failures:
        print("  - %s / %s: %s %s" % (r["cat"], r["desc"], r["status"], r["note"]))
    print("Informe guardado en: %s" % write_report(results, started))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
