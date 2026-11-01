"""Manejo de errores de validación común a todos los servicios.

Por defecto FastAPI devuelve en el 422 el valor recibido ('input'). Si ese valor es NaN o
Infinity, el JSON de respuesta no se puede serializar y el cliente recibe un 500.
Este manejador devuelve solo dónde está el error, qué pasó y de qué tipo es.
"""
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_handler(request, exc):
    errors = [
        {"loc": [str(part) for part in e.get("loc", ())], "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


def install_error_handlers(app):
    app.add_exception_handler(RequestValidationError, validation_handler)
