"""Mensajería con RabbitMQ: publicar, consumir y patrón Outbox.

- Un exchange 'topic' durable llamado 'academico'.
- Cada servicio consumidor declara su propia cola durable: si el servicio se cae,
  los mensajes esperan en el broker y se entregan al reiniciar.
- Outbox: el evento se guarda en la MISMA transacción que el cambio de datos y se
  publica después. Si el broker está caído, un hilo reintenta cada pocos segundos.
  La entrega es "al menos una vez": los consumidores deben ser idempotentes.
"""
import json
import logging
import os
import threading
import time

import pika

from common.db import get_db

log = logging.getLogger("events")

RABBIT_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBIT_USER = os.getenv("RABBITMQ_USER", "guest")
RABBIT_PASS = os.getenv("RABBITMQ_PASS", "guest")
EXCHANGE = "academico"


def _connect():
    params = pika.ConnectionParameters(
        host=RABBIT_HOST,
        credentials=pika.PlainCredentials(RABBIT_USER, RABBIT_PASS),
        heartbeat=30,
        connection_attempts=1,
        socket_timeout=3,
        blocked_connection_timeout=10,
    )
    return pika.BlockingConnection(params)


def publish(routing_key, payload):
    """Publica un evento. Devuelve True si el broker lo recibió."""
    try:
        conn = _connect()
        try:
            ch = conn.channel()
            ch.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
            ch.basic_publish(
                exchange=EXCHANGE,
                routing_key=routing_key,
                body=json.dumps(payload).encode(),
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )
            return True
        finally:
            conn.close()
    except Exception as exc:  # broker caído, red, etc.
        log.warning("No se pudo publicar '%s': %s", routing_key, exc)
        return False


def start_consumer(queue, binding_keys, handler):
    """Consume eventos en un hilo de fondo y se reconecta solo si el broker cae.

    handler(routing_key, payload). Si lanza excepción, el mensaje se reencola.
    """

    def run():
        while True:
            try:
                conn = _connect()
                ch = conn.channel()
                ch.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
                ch.queue_declare(queue=queue, durable=True)
                for key in binding_keys:
                    ch.queue_bind(queue=queue, exchange=EXCHANGE, routing_key=key)
                ch.basic_qos(prefetch_count=10)

                def on_message(channel, method, properties, body):
                    try:
                        payload = json.loads(body)
                    except ValueError:
                        log.error("Mensaje inválido descartado: %r", body[:200])
                        channel.basic_ack(method.delivery_tag)
                        return
                    try:
                        handler(method.routing_key, payload)
                        channel.basic_ack(method.delivery_tag)
                    except Exception as exc:
                        log.warning("Handler falló para %s (%s); se reintenta", method.routing_key, exc)
                        time.sleep(3)
                        channel.basic_nack(method.delivery_tag, requeue=True)

                ch.basic_consume(queue=queue, on_message_callback=on_message)
                log.info("Consumidor '%s' escuchando %s", queue, binding_keys)
                ch.start_consuming()
            except Exception as exc:
                log.warning("Consumidor '%s' sin conexión (%s); reintento en 5 s", queue, exc)
                time.sleep(5)

    threading.Thread(target=run, daemon=True, name=f"consumer-{queue}").start()


class Outbox:
    DDL = (
        "CREATE TABLE IF NOT EXISTS outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, routing_key TEXT NOT NULL, "
        "payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0)"
    )

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()

    def init(self):
        with get_db(self.db_path) as c:
            c.execute(self.DDL)

    @staticmethod
    def add(conn, routing_key, payload):
        """Guarda el evento usando la conexión (transacción) de quien llama."""
        conn.execute(
            "INSERT INTO outbox(routing_key, payload) VALUES (?, ?)",
            (routing_key, json.dumps(payload)),
        )

    def flush(self):
        """Publica los eventos pendientes en orden. Se detiene al primer fallo."""
        with self._lock:
            with get_db(self.db_path) as c:
                rows = c.execute(
                    "SELECT id, routing_key, payload FROM outbox WHERE sent = 0 ORDER BY id"
                ).fetchall()
                for row in rows:
                    if not publish(row["routing_key"], json.loads(row["payload"])):
                        break
                    c.execute("UPDATE outbox SET sent = 1 WHERE id = ?", (row["id"],))
                    c.commit()

    def flush_async(self):
        threading.Thread(target=self._safe_flush, daemon=True).start()

    def _safe_flush(self):
        try:
            self.flush()
        except Exception as exc:
            log.warning("Error en flush del outbox: %s", exc)

    def start_flusher(self, interval=5.0):
        def loop():
            while True:
                time.sleep(interval)
                self._safe_flush()

        threading.Thread(target=loop, daemon=True, name="outbox-flusher").start()
