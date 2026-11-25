"""
Almacén de los borradores: Redis de Upstash (API REST, la que conecta Vercel → Storage).

Claves:
  b:<id>            borrador (JSON)
  r:<codigo>        ids de los borradores de una reserva (conjunto)
  pend              ids pendientes de decisión (conjunto)
  tg:<chat>:<msg>   id del borrador de una tarjeta de Telegram (para editar respondiendo a ella)
  lock:<id>         envío en curso (evita enviar dos veces)
"""
from __future__ import annotations

import json
import os
import time

import requests

TTL_S = 90 * 24 * 3600


class ErrorAlmacen(RuntimeError):
    pass


class Upstash:
    def __init__(self, url: str, token: str, sesion: requests.Session | None = None):
        self.url, self.token = url.rstrip("/"), token
        self.sesion = sesion or requests.Session()

    @classmethod
    def desde_entorno(cls) -> Upstash:
        url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
        token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            raise ErrorAlmacen("Falta la base de datos: conecta Upstash Redis al proyecto (Vercel → Storage).")
        return cls(url, token)

    def cmd(self, *args):
        r = self.sesion.post(self.url, json=[str(a) for a in args], headers={"Authorization": f"Bearer {self.token}"}, timeout=10)
        if r.status_code >= 300:
            raise ErrorAlmacen(f"Upstash HTTP {r.status_code}: {r.text[:200]}")
        datos = r.json()
        if "error" in datos:
            raise ErrorAlmacen(f"Upstash: {datos['error']}")
        return datos.get("result")


class Memoria:
    """El mismo almacén en memoria (pruebas)."""

    def __init__(self):
        self.datos: dict = {}
        self.caduca: dict = {}

    def _vivo(self, k):
        if k in self.caduca and self.caduca[k] < time.time():
            self.datos.pop(k, None)
            self.caduca.pop(k, None)
        return self.datos.get(k)

    def cmd(self, op, *a):
        op = op.upper()
        if op == "GET":
            v = self._vivo(a[0])
            return v if isinstance(v, str) else None
        if op == "SET":
            k, v, *opc = a
            opc = [str(x).upper() for x in opc]
            if "NX" in opc and self._vivo(k) is not None:
                return None
            self.datos[k] = str(v)
            if "EX" in opc:
                self.caduca[k] = time.time() + int(opc[opc.index("EX") + 1])
            return "OK"
        if op == "DEL":
            return sum(1 for k in a if self.datos.pop(k, None) is not None)
        if op == "SADD":
            s = self.datos.setdefault(a[0], set())
            antes = len(s)
            s.update(map(str, a[1:]))
            return len(s) - antes
        if op == "SREM":
            s = self.datos.get(a[0], set())
            n = sum(1 for x in a[1:] if str(x) in s)
            s.difference_update(map(str, a[1:]))
            return n
        if op == "SMEMBERS":
            return sorted(self._vivo(a[0]) or [])
        if op == "EXPIRE":
            self.caduca[a[0]] = time.time() + int(a[1])
            return 1
        if op == "PING":
            return "PONG"
        raise NotImplementedError(op)


class Almacen:
    def __init__(self, redis):
        self.r = redis

    def crear(self, b: dict) -> bool:
        """False si ya existía un borrador con ese id (la creación es idempotente)."""
        if self.r.cmd("SET", f"b:{b['id']}", json.dumps(b, ensure_ascii=False), "NX", "EX", TTL_S) is None:
            return False
        if b.get("codigo"):
            self.r.cmd("SADD", f"r:{b['codigo']}", b["id"])
            self.r.cmd("EXPIRE", f"r:{b['codigo']}", TTL_S)
        self.r.cmd("SADD", "pend", b["id"])
        return True

    def leer(self, id_: str) -> dict | None:
        v = self.r.cmd("GET", f"b:{id_}")
        return json.loads(v) if v else None

    def guardar(self, b: dict) -> None:
        self.r.cmd("SET", f"b:{b['id']}", json.dumps(b, ensure_ascii=False), "EX", TTL_S)
        if b["estado"] in ("pendiente", "error"):
            self.r.cmd("SADD", "pend", b["id"])
        else:
            self.r.cmd("SREM", "pend", b["id"])

    def de_reserva(self, codigo: str) -> list[dict]:
        return [b for b in (self.leer(i) for i in self.r.cmd("SMEMBERS", f"r:{codigo}") or []) if b]

    def pendientes(self) -> list[dict]:
        res = []
        for i in self.r.cmd("SMEMBERS", "pend") or []:
            b = self.leer(i)
            if b and b["estado"] in ("pendiente", "error"):
                res.append(b)
            else:
                self.r.cmd("SREM", "pend", i)
        return sorted(res, key=lambda b: b.get("creado", ""))

    def tarjeta(self, chat_id: int, message_id: int, id_: str) -> None:
        self.r.cmd("SET", f"tg:{chat_id}:{message_id}", id_, "EX", TTL_S)

    def de_tarjeta(self, chat_id: int, message_id: int) -> str | None:
        return self.r.cmd("GET", f"tg:{chat_id}:{message_id}")

    def bloquear(self, id_: str, segundos: int = 600) -> bool:
        return self.r.cmd("SET", f"lock:{id_}", "1", "NX", "EX", segundos) is not None

    def desbloquear(self, id_: str) -> None:
        self.r.cmd("DEL", f"lock:{id_}")

    def ping(self) -> bool:
        return self.r.cmd("PING") == "PONG"
