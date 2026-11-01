"""Circuit Breaker: evita seguir llamando a un servicio que ya sabemos que está caído."""
import threading
import time


class CircuitOpenError(Exception):
    """El circuito está abierto: no se intenta la llamada."""


class CircuitBreaker:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold=3, reset_timeout=10.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self):
        return self._state

    def call(self, fn, *args, **kwargs):
        with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._opened_at >= self.reset_timeout:
                    self._state = self.HALF_OPEN  # deja pasar una prueba
                else:
                    raise CircuitOpenError("circuito abierto")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

    def _record_failure(self):
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()

    def _record_success(self):
        with self._lock:
            self._failures = 0
            self._state = self.CLOSED
