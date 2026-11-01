"""Demostración de punta a punta del flujo de matrícula (solo usa la librería estándar).

Uso:  python scripts/demo.py
Requiere el sistema corriendo:  docker compose up --build
Para repetirla desde cero:      docker compose down -v
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except urllib.error.URLError as e:
        print(f"\nNo se pudo conectar con {BASE}: {e.reason}")
        print("¿Está corriendo 'docker compose up --build'?")
        sys.exit(1)


def login(username, password):
    status, body = call("POST", "/auth/login", body={"username": username, "password": password})
    if status != 200:
        print(f"Login falló para {username}: {status} {body}")
        sys.exit(1)
    return body["access_token"]


def wait_for(description, fn, timeout=25):
    """Repite fn() hasta que devuelva algo verdadero (los eventos son asíncronos)."""
    end = time.time() + timeout
    while time.time() < end:
        result = fn()
        if result:
            return result
        time.sleep(1)
    print(f"  (se agotó el tiempo esperando: {description})")
    return None


def step(title):
    print(f"\n=== {title}")


def available(token, course_id):
    _, courses = call("GET", "/courses", token)
    return next(c["available"] for c in courses if c["id"] == course_id)


def main():
    step("Login")
    est1 = login("estudiante1", "est123")
    est2 = login("estudiante2", "est123")
    print("  estudiante1 y estudiante2 autenticados (JWT emitido por Auth)")

    step("Flujo feliz: matrícula + pago confirmado (estudiante1, curso 1)")
    status, enr = call("POST", "/enrollments", est1, {"course_id": 1})
    print(f"  POST /enrollments -> {status} {enr}")
    if status == 409:
        print("  Ya existe una matrícula. Ejecuta 'docker compose down -v' para empezar de cero.")
    elif status == 201:
        pay = wait_for("cobro pendiente en Payment", lambda: next(
            (p for p in call("GET", "/payments?status=PENDIENTE", est1)[1] if p["enrollment_id"] == enr["id"]), None))
        if pay:
            pay_id = pay["id"]
            print("  Payment creó el cobro #%s por $%s (evento matricula.creada)" % (pay_id, format(pay["amount"], ",.0f")))
            result = call("POST", "/payments/%s/pay" % pay_id, est1, {})
            print("  POST /payments/%s/pay -> %s" % (pay_id, result))
            final = wait_for("matrícula CONFIRMADA", lambda: (
                lambda e: e if e["status"] == "CONFIRMADA" else None)(call("GET", f"/enrollments/{enr['id']}", est1)[1]))
            print(f"  Estado final de la matrícula: {final['status'] if final else 'no confirmada'}")

    step("Compensación: pago fallido libera el cupo (estudiante2, curso 3 con 2 cupos)")
    before = available(est2, 3)
    print(f"  Cupos disponibles antes: {before}")
    status, enr = call("POST", "/enrollments", est2, {"course_id": 3})
    print(f"  POST /enrollments -> {status} {enr}")
    if status == 201:
        print(f"  Cupos tras reservar: {available(est2, 3)}")
        pay = wait_for("cobro pendiente", lambda: next(
            (p for p in call("GET", "/payments?status=PENDIENTE", est2)[1] if p["enrollment_id"] == enr["id"]), None))
        if pay:
            pay_id = pay["id"]
            result = call("POST", "/payments/%s/pay" % pay_id, est2, {"simulate_failure": True})
            print("  Pago con falla simulada -> %s" % (result,))
            wait_for("matrícula CANCELADA", lambda: call("GET", f"/enrollments/{enr['id']}", est2)[1]["status"] == "CANCELADA")
            state = call("GET", f"/enrollments/{enr['id']}", est2)[1]
            print(f"  Estado final: {state['status']} (motivo: {state['reason']})")
            wait_for("cupo liberado", lambda: available(est2, 3) == before)
            print(f"  Cupos después de la compensación: {available(est2, 3)}")

    step("Aggregator: resumen del estudiante (Student + Course + Payment en una sola respuesta)")
    status, summary = call("GET", "/enrollments/me/summary", est1)
    print("  GET /enrollments/me/summary -> %s" % status)
    if status == 200:
        perfil = summary["student"]["full_name"] if summary["student"] else "no disponible"
        pagos = len(summary["pending_payments"]) if summary["pending_payments"] is not None else "no disponible"
        print("  Perfil: %s" % perfil)
        print("  Matrículas: %s" % [(e["course_name"], e["status"]) for e in summary["enrollments"]])
        print("  Pagos pendientes: %s" % pagos)
        print("  Servicios que no respondieron: %s" % (summary["unavailable"] or "ninguno"))

    step("Branch: el docente consulta a Enrollment sus cursos y estudiantes")
    doc = login("docente1", "doc123")
    status, courses = call("GET", "/teachers/me/courses", doc)
    print("  GET /teachers/me/courses -> %s" % status)
    for c in courses if status == 200 else []:
        print("    curso %s (%s): %s matriculados" % (c["course_id"], c["course_name"], c["enrolled_students"]))
    status, students = call("GET", "/teachers/me/courses/1/students", doc)
    print("  GET /teachers/me/courses/1/students -> %s %s" % (status, students))
    if status == 200 and students:
        eid = students[0]["enrollment_id"]
        result = call("POST", "/teachers/me/courses/1/grades", doc, {"enrollment_id": eid, "grade": 4.5, "comment": "Buen trabajo"})
        print("  Registrar nota de la matrícula %s -> %s" % (eid, result))

    step("Notificaciones de estudiante2 (las generó Notification solo con eventos)")
    for n in reversed(call("GET", "/notifications", est2)[1] or []):
        print(f"  [{n['event']}] {n['message']}")

    step("Seguridad")
    sin_token = call("GET", "/courses")[0]
    interna = call("POST", "/internal/courses/1/reserve", est1, {"enrollment_id": 1})[0]
    nuevo = {"code": "X", "name": "X", "credits": 1, "price": 1, "capacity": 1}
    sin_permiso = call("POST", "/courses", est1, nuevo)[0]
    print("  Sin token             -> %s (esperado 401)" % sin_token)
    print("  Ruta interna          -> %s (esperado 404)" % interna)
    print("  Estudiante crea curso -> %s (esperado 403)" % sin_permiso)
    est_docente = call("GET", "/teachers/me/courses", est1)[0]
    print("  Estudiante usa rutas de docente -> %s (esperado 403)" % est_docente)
    print("\nListo.")


if __name__ == "__main__":
    main()
