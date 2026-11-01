"""Chaos Monkey: apaga un servicio, observa cómo reacciona el sistema y lo vuelve a levantar.

Cada escenario crea su propio curso de prueba, así que se puede repetir sin 'docker compose down -v'.

Uso:
  python scripts/chaos.py --listar              lista los escenarios
  python scripts/chaos.py --servicio payment    un escenario
  python scripts/chaos.py --todos               todos, en orden aleatorio (10-15 minutos)
  python scripts/chaos.py --azar 3              3 escenarios al azar

Requiere:  docker compose up --build  corriendo, y ejecutarlo desde cualquier carpeta.
Salida:    resultados/chaos_<fecha>.md
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime

import common_http as http

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDS = []
CURRENT = {"name": ""}


# ---------- infraestructura (se reemplaza en las pruebas) ----------
def now():
    return time.time()


def sleep(seconds):
    time.sleep(seconds)


def api(method, path, token=None, body=None):
    return http.request(method, path, token=token, body=body)


def compose(*args, timeout=180):
    try:
        p = subprocess.run(["docker", "compose"] + list(args), cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise SystemExit("No se encontró el comando 'docker'. Abre Docker Desktop e inténtalo de nuevo.")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def breaker_state(service, key):
    """Lee el estado del Circuit Breaker desde el /health interno del servicio."""
    code = "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
    rc, out = compose("exec", "-T", service, "python", "-c", code, timeout=40)
    for line in reversed(out.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line).get(key)
            except ValueError:
                return None
    return None


# ---------- utilidades ----------
def rec(step, expected, observed, ok):
    RECORDS.append({"scenario": CURRENT["name"], "step": step, "expected": expected,
                    "observed": observed, "ok": bool(ok)})
    print("  [%s] %s\n        esperado: %s\n        observado: %s" % ("OK " if ok else "XX ", step, expected, observed))


def wait_until(fn, timeout=60, interval=2):
    start = now()
    while True:
        try:
            value = fn()
        except Exception:
            value = None
        if value:
            return value, now() - start
        if now() - start >= timeout:
            return None, now() - start
        sleep(interval)


def stop(service):
    print("    $ docker compose stop %s" % service)
    rc, out = compose("stop", service)
    if rc != 0:
        raise RuntimeError("No se pudo detener %s: %s" % (service, out.strip()[:200]))


def start(service):
    print("    $ docker compose start %s" % service)
    compose("start", service)


def field(body, key, default=None):
    return body.get(key, default) if isinstance(body, dict) else default


class Ctx:
    def __init__(self):
        self.est = http.login("estudiante1", "est123")
        self.doc = http.login("docente1", "doc123")
        self.adm = http.login("admin", "admin123")
        self.n = 0

    def new_course(self, capacity=5):
        self.n += 1
        code = "CH%d%d" % (int(now()) % 10 ** 8, self.n)
        s, c, _ = api("POST", "/courses", self.adm,
                      {"code": code, "name": "Curso Caos %s" % code, "credits": 1, "price": 100000, "capacity": capacity})
        if s != 201:
            raise RuntimeError("No se pudo crear el curso de prueba (%s %s)" % (s, c))
        return c

    def enroll(self, course_id):
        return api("POST", "/enrollments", self.est, {"course_id": course_id})

    def pending_payment(self, enrollment_id):
        s, b, _ = api("GET", "/payments?status=PENDIENTE", self.est)
        if s == 200 and isinstance(b, list):
            return next((p for p in b if p.get("enrollment_id") == enrollment_id), None)
        return None

    def status_of(self, enrollment_id):
        s, b, _ = api("GET", "/enrollments/%s" % enrollment_id, self.est)
        return field(b, "status") if s == 200 else None

    def summary(self):
        return api("GET", "/enrollments/me/summary", self.est)

    def available(self, course_id):
        s, b, _ = api("GET", "/courses/%s" % course_id, self.est)
        return field(b, "available") if s == 200 else None


# ---------- escenarios ----------
def sc_payment(ctx):
    c = ctx.new_course()
    stop("payment")
    s, e, _ = ctx.enroll(c["id"])
    rec("Matricular con Payment apagado", "201 y estado PENDIENTE_PAGO (Enrollment no llama a Payment)",
        "%s %s" % (s, field(e, "status", e)), s == 201 and field(e, "status") == "PENDIENTE_PAGO")
    eid = field(e, "id")
    s, _, t = api("GET", "/payments", ctx.est)
    rec("Consultar pagos con Payment apagado", "503 (servicio no disponible)", "%s (%.1f s)" % (s, t), s == 503)
    s, b, _ = ctx.summary()
    rec("Resumen del estudiante (Aggregator)", "200 y 'payments' en unavailable",
        "%s unavailable=%s" % (s, field(b, "unavailable")), s == 200 and "payments" in (field(b, "unavailable") or []))
    start("payment")
    pay, secs = wait_until(lambda: ctx.pending_payment(eid), 60)
    rec("Cobro creado tras reiniciar Payment (el evento esperó en la cola)", "el cobro aparece",
        ("aparece a los %.0f s" % secs) if pay else "no apareció en 60 s", pay)
    if pay:
        api("POST", "/payments/%s/pay" % pay["id"], ctx.est, {})
        ok, secs = wait_until(lambda: ctx.status_of(eid) == "CONFIRMADA", 45)
        rec("Pagar y confirmar la matrícula", "CONFIRMADA", ("CONFIRMADA a los %.0f s" % secs) if ok else "no se confirmó", ok)


def sc_notification(ctx):
    c = ctx.new_course()
    stop("notification")
    s, e, _ = ctx.enroll(c["id"])
    rec("Matricular con Notification apagado", "201 (nadie depende de Notification)", str(s), s == 201)
    s, _, t = api("GET", "/notifications", ctx.est)
    rec("Consultar notificaciones con el servicio apagado", "503", "%s (%.1f s)" % (s, t), s == 503)
    start("notification")

    def delivered():
        s, b, _ = api("GET", "/notifications", ctx.est)
        return s == 200 and any(c["name"] in n.get("message", "") for n in b)

    ok, secs = wait_until(delivered, 60)
    rec("Aviso entregado tras reiniciar (esperó en la cola durable)", "el aviso aparece",
        ("aparece a los %.0f s" % secs) if ok else "no apareció en 60 s", ok)


def sc_course(ctx):
    c = ctx.new_course()
    stop("course")
    s, _, t = api("GET", "/courses", ctx.est)
    rec("Consultar cursos con Course apagado", "503", "%s (%.1f s)" % (s, t), s == 503)
    codes = []
    for _ in range(4):
        s, _, _ = ctx.enroll(c["id"])
        codes.append(s)
    rec("Matricular 4 veces con Course apagado", "503 las 4 veces, sin colgar", str(codes), codes == [503] * 4)
    state = breaker_state("enrollment", "course_breaker")
    rec("Circuit Breaker de Enrollment hacia Course", "OPEN", str(state), state == "OPEN")
    s, lst, _ = api("GET", "/enrollments", ctx.est)
    stuck = [e for e in (lst if isinstance(lst, list) else []) if e.get("course_id") == c["id"]
             and e.get("status") in ("SOLICITADA", "PENDIENTE_PAGO")]
    rec("Matrículas colgadas por la caída", "ninguna (quedan CANCELADAS)", "%d colgadas" % len(stuck), s == 200 and not stuck)
    start("course")
    ok, secs = wait_until(lambda: ctx.enroll(c["id"])[0] == 201, 60, interval=3)
    rec("Recuperación: matricular de nuevo", "201 (el circuito se cierra tras ~10 s)",
        ("201 a los %.0f s" % secs) if ok else "no se recuperó en 60 s", ok)


def sc_saga_course(ctx):
    c = ctx.new_course(capacity=2)
    s, e, _ = ctx.enroll(c["id"])
    eid = field(e, "id")
    rec("Matricular (reserva 1 de 2 cupos)", "201", str(s), s == 201)
    pay, _ = wait_until(lambda: ctx.pending_payment(eid), 30)
    rec("Cobro pendiente creado", "existe", "sí" if pay else "no", pay)
    if not pay:
        return
    stop("course")
    s, _, _ = api("POST", "/payments/%s/pay" % pay["id"], ctx.est, {"simulate_failure": True})
    rec("Pago fallido con Course apagado", "200 (el pago se registra como FALLIDO)", str(s), s == 200)
    sleep(10)
    st = ctx.status_of(eid)
    rec("Compensación pendiente mientras Course está caído", "PENDIENTE_PAGO (la compensación espera a que vuelva Course)",
        str(st), st == "PENDIENTE_PAGO")
    start("course")
    ok, secs = wait_until(lambda: ctx.status_of(eid) == "CANCELADA", 90, interval=3)
    rec("Compensación al volver Course", "CANCELADA", ("CANCELADA a los %.0f s" % secs) if ok else "sigue sin cancelarse", ok)
    free, _ = wait_until(lambda: ctx.available(c["id"]) == 2, 30)
    rec("Cupo liberado", "2 de 2 disponibles", str(ctx.available(c["id"])), free)


def sc_student(ctx):
    c = ctx.new_course()
    stop("student")
    results = [ctx.summary() for _ in range(4)]
    s, b, _ = results[-1]
    rec("Resumen con Student apagado", "200 parcial: student=null y 'student' en unavailable",
        "%s student=%s unavailable=%s" % (s, field(b, "student"), field(b, "unavailable")),
        s == 200 and field(b, "student") is None and "student" in (field(b, "unavailable") or []))
    rec("El resumen conserva las matrículas propias", "lista de matrículas presente", "%d matrículas" % len(field(b, "enrollments", [])),
        isinstance(field(b, "enrollments"), list))
    state = breaker_state("enrollment", "student_breaker")
    rec("Circuit Breaker del Aggregator hacia Student", "OPEN", str(state), state == "OPEN")
    s, _, _ = ctx.enroll(c["id"])
    rec("Matricular con Student apagado", "201 (no depende de Student)", str(s), s == 201)
    start("student")
    ok, secs = wait_until(lambda: field(ctx.summary()[1], "unavailable") == [], 60, interval=3)
    rec("Recuperación del resumen completo", "unavailable vacío", ("completo a los %.0f s" % secs) if ok else "no se recuperó", ok)


def sc_enrollment(ctx):
    stop("enrollment")
    s, b, _ = api("GET", "/teachers/me/courses", ctx.doc)
    flags = [c.get("students_available") for c in b] if isinstance(b, list) else None
    rec("Docente consulta sus cursos con Enrollment apagado", "200 con students_available=false",
        "%s %s" % (s, flags), s == 200 and bool(flags) and all(f is False for f in flags))
    s, _, _ = api("GET", "/teachers/me/courses/1/students", ctx.doc)
    rec("Docente pide la lista de estudiantes", "503", str(s), s == 503)
    c = ctx.new_course()
    s, _, t = ctx.enroll(c["id"])
    rec("Estudiante intenta matricularse", "503", "%s (%.1f s)" % (s, t), s == 503)
    s, _, _ = api("GET", "/courses", ctx.est)
    rec("Consultar cursos (Course no depende de Enrollment)", "200", str(s), s == 200)
    start("enrollment")

    def recovered():
        s, b, _ = api("GET", "/teachers/me/courses", ctx.doc)
        return s == 200 and isinstance(b, list) and b and all(c.get("students_available") for c in b)

    ok, secs = wait_until(recovered, 60, interval=3)
    rec("Recuperación del docente", "students_available=true", ("recuperado a los %.0f s" % secs) if ok else "no se recuperó", ok)


def sc_auth(ctx):
    stop("auth")
    s, _, t = api("POST", "/auth/login", None, {"username": "estudiante1", "password": "est123"})
    rec("Login nuevo con Auth apagado", "503", "%s (%.1f s)" % (s, t), s == 503)
    s, _, _ = api("GET", "/courses", ctx.est)
    rec("Token ya emitido sigue funcionando (el Gateway lo valida solo)", "200", str(s), s == 200)
    start("auth")
    ok, secs = wait_until(lambda: api("POST", "/auth/login", None, {"username": "estudiante1", "password": "est123"})[0] == 200, 60)
    rec("Recuperación del login", "200", ("200 a los %.0f s" % secs) if ok else "no se recuperó", ok)


def sc_rabbitmq(ctx):
    c = ctx.new_course()
    stop("rabbitmq")
    s, e, _ = ctx.enroll(c["id"])
    eid = field(e, "id")
    rec("Matricular con el broker apagado", "201 y PENDIENTE_PAGO (el evento queda en el outbox)",
        "%s %s" % (s, field(e, "status", e)), s == 201 and field(e, "status") == "PENDIENTE_PAGO")
    sleep(8)
    none_yet = ctx.pending_payment(eid) is None
    rec("Sin broker no llega el evento a Payment", "aún no hay cobro", "no hay cobro" if none_yet else "ya hay cobro", none_yet)
    start("rabbitmq")
    pay, secs = wait_until(lambda: ctx.pending_payment(eid), 120, interval=3)
    rec("El evento se publica al volver el broker (outbox)", "el cobro aparece",
        ("aparece a los %.0f s" % secs) if pay else "no apareció en 120 s", pay)


def sc_gateway(ctx):
    stop("gateway")
    s, b, _ = api("GET", "/courses", ctx.est)
    rec("Acceso al sistema con el Gateway apagado", "sin acceso (instancia única: limitación conocida)",
        "sin respuesta" if s is None else "respondió %s" % s, s is None)
    start("gateway")
    ok, secs = wait_until(lambda: api("GET", "/courses", ctx.est)[0] == 200, 60)
    rec("Recuperación del acceso", "200", ("200 a los %.0f s" % secs) if ok else "no se recuperó", ok)


def sc_teacher(ctx):
    stop("teacher")
    s, _, t = api("GET", "/teachers/me/courses", ctx.doc)
    rec("Docente consulta con Teacher apagado", "503", "%s (%.1f s)" % (s, t), s == 503)
    c = ctx.new_course()
    s, _, _ = ctx.enroll(c["id"])
    rec("Matricular con Teacher apagado (no depende de él)", "201", str(s), s == 201)
    start("teacher")
    ok, secs = wait_until(lambda: api("GET", "/teachers/me/courses", ctx.doc)[0] == 200, 60)
    rec("Recuperación", "200", ("200 a los %.0f s" % secs) if ok else "no se recuperó", ok)


# nombre -> (función, contenedor que se apaga)
SCENARIOS = {
    "payment": (sc_payment, "payment"),
    "notification": (sc_notification, "notification"),
    "course": (sc_course, "course"),
    "saga-course": (sc_saga_course, "course"),
    "student": (sc_student, "student"),
    "enrollment": (sc_enrollment, "enrollment"),
    "auth": (sc_auth, "auth"),
    "rabbitmq": (sc_rabbitmq, "rabbitmq"),
    "gateway": (sc_gateway, "gateway"),
    "teacher": (sc_teacher, "teacher"),
}


def settle(ctx):
    """Deja el sistema sano antes del siguiente escenario (los Circuit Breakers tardan ~10 s en reabrirse)."""
    sleep(12)
    wait_until(lambda: api("GET", "/courses", ctx.est)[0] == 200, 90, interval=3)
    wait_until(lambda: ctx.enroll(ctx.new_course()["id"])[0] == 201, 60, interval=3)


def run_scenario(name, ctx):
    fn, target = SCENARIOS[name]
    CURRENT["name"] = name
    print("\n=== Escenario: %s ===" % name)
    try:
        fn(ctx)
    except Exception as exc:  # un error del script no debe dejar servicios apagados
        rec("ejecución del escenario", "sin errores", "error: %s" % exc, False)
    finally:
        start(target)
    try:
        settle(ctx)
    except Exception as exc:
        print("  (aviso al estabilizar: %s)" % exc)


def write_report(names, started):
    os.makedirs(os.path.join(ROOT, "resultados"), exist_ok=True)
    path = os.path.join(ROOT, "resultados", "chaos_%s.md" % started.strftime("%Y%m%d_%H%M%S"))
    total, good = len(RECORDS), sum(r["ok"] for r in RECORDS)
    lines = ["# Chaos Monkey", "", "Fecha: %s" % started.strftime("%Y-%m-%d %H:%M"),
             "Escenarios: %s" % ", ".join(names), "", "Comprobaciones: %d. Cumplidas: %d. No cumplidas: %d." % (total, good, total - good), ""]
    for name in names:
        lines += ["## %s" % name, "", "| Paso | Esperado | Observado | Resultado |", "|---|---|---|---|"]
        for r in RECORDS:
            if r["scenario"] == name:
                lines.append("| %s | %s | %s | %s |" % (r["step"].replace("|", "/"), r["expected"].replace("|", "/"),
                                                         r["observed"].replace("|", "/"), "OK" if r["ok"] else "NO CUMPLE"))
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def main():
    parser = argparse.ArgumentParser(description="Chaos Monkey para la plataforma de gestión académica")
    parser.add_argument("--listar", action="store_true", help="lista los escenarios")
    parser.add_argument("--servicio", nargs="+", choices=sorted(SCENARIOS), help="escenario(s) a ejecutar")
    parser.add_argument("--todos", action="store_true", help="todos los escenarios en orden aleatorio")
    parser.add_argument("--azar", type=int, metavar="N", help="N escenarios elegidos al azar")
    parser.add_argument("--base", default=http.BASE, help="URL del Gateway (por defecto %(default)s)")
    args = parser.parse_args()
    http.BASE = args.base

    if args.listar or not (args.servicio or args.todos or args.azar):
        print("Escenarios disponibles:")
        for name, (fn, target) in SCENARIOS.items():
            print("  %-13s apaga '%s'" % (name, target))
        print("\nEjemplo: python scripts/chaos.py --servicio payment")
        return
    names = list(SCENARIOS) if args.todos else (args.servicio or random.sample(list(SCENARIOS), min(args.azar, len(SCENARIOS))))
    if args.todos or args.azar:
        random.shuffle(names)

    rc, out = compose("version")
    if rc != 0:
        sys.exit("'docker compose' no responde: %s" % out.strip()[:200])
    print("Asegurando que todos los servicios estén arriba...")
    compose("up", "-d")
    try:
        ctx = Ctx()
    except RuntimeError as exc:
        sys.exit(str(exc))
    wait_until(lambda: api("GET", "/courses", ctx.est)[0] == 200, 90, interval=3)

    started = datetime.now()
    for name in names:
        run_scenario(name, ctx)
    good = sum(r["ok"] for r in RECORDS)
    print("\n=== Resumen: %d comprobaciones | cumplidas %d | no cumplidas %d" % (len(RECORDS), good, len(RECORDS) - good))
    for r in RECORDS:
        if not r["ok"]:
            print("  - [%s] %s -> observado: %s" % (r["scenario"], r["step"], r["observed"]))
    print("Informe guardado en: %s" % write_report(names, started))


if __name__ == "__main__":
    main()
