"""Límites compartidos de validación."""

# SQLite guarda enteros de 64 bits con signo. Un id fuera de este rango haría fallar
# la consulta con un error 500 en vez de una respuesta 422 controlada.
MAX_ID = 2 ** 63 - 1
