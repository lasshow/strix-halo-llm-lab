#!/usr/bin/env python3
"""La UNICA espera de servicio del laboratorio.

Por que existe (auditoria externa de la campana H-031, fallo B):

  Habia dos esperas distintas y una de las dos estaba mal.
  `bench-ubatch.py::espera_salud` ya exigia unidad activa Y /health 200, en ese
  orden, porque H-024 y H-025 demostraron que un 200 en el puerto no acredita
  que la unidad que te importa este arriba. `campana-nocturna.py::espera_prod`
  volvia a la version ingenua: devolvia True con /health=200 y solo consultaba
  `is-active` DESPUES, para decidir si abandonar. Con eso, cualquier proceso
  escuchando en el puerto 8080 -- incluido un servidor de banco mal apagado --
  daba verde a "produccion restaurada".

  Duplicar una espera es duplicar el sitio donde se puede volver a equivocar,
  asi que aqui hay una sola, y las tres condiciones son simultaneas:

      1. `systemctl is-active <unidad>` == "active"
      2. `/health` responde 200
      3. `/v1/models` (con la clave) sirve el modelo esperado

  La tercera es nueva y no es cosmetica: (1) y (2) juntas siguen aceptando una
  unidad sana que cargo OTRO modelo -- exactamente lo que pasa si una campana
  deja la unidad apuntando a un GGUF de pruebas. Se comprueba solo si se pide
  un `modelo`; sin el, la espera se queda en las dos primeras.

Una unidad en `failed` NO es "todavia no": es instrumental caido, y por eso
lanza `ErrorInfraestructura` en lugar de devolver False tras agotar el limite.
Quien prefiera el booleano de siempre que capture la excepcion.

`espera_proceso` es la misma espera para un servidor de BANCO, que no es una
unidad de systemd sino un proceso hijo nuestro: ahi la condicion (1) no puede
ser `systemctl is-active` y pasa a ser "el proceso sigue vivo". Las otras dos
son identicas, y por el mismo motivo: sin comprobar el modelo, un servidor de
banco que cargo otro GGUF pasaria por bueno.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validacion import ErrorInfraestructura  # noqa: E402

SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")


def _sh(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()


def _abrir(url: str, timeout: float, cabeceras: dict | None = None):
    req = urllib.request.Request(url, headers=cabeceras or {})
    return urllib.request.urlopen(req, timeout=timeout)


def modelos_servidos(puerto: int, clave: str | None = None, timeout: float = 10,
                     abrir=None) -> list[str]:
    """Ids de /v1/models. Lista vacia si no se puede leer (no es un fallo aqui)."""
    abrir = abrir or _abrir
    cab = {"Authorization": f"Bearer {clave}"} if clave else {}
    try:
        with abrir(f"http://127.0.0.1:{puerto}/v1/models", timeout, cab) as r:
            if getattr(r, "status", 200) != 200:
                return []
            d = json.loads(r.read())
    except Exception:
        return []
    datos = d.get("data") if isinstance(d, dict) else None
    if not isinstance(datos, list):
        return []
    return [m.get("id") for m in datos if isinstance(m, dict) and m.get("id")]


def espera_servicio(unidad: str, puerto: int, modelo: str | None = None,
                    limite: float = 600, clave: str | None = None,
                    sh=None, abrir=None, pausa: float = 5,
                    traza=print) -> bool:
    """Espera a que ESA unidad sirva ESE modelo en ESE puerto.

    Devuelve True cuando se cumplen las tres condiciones a la vez, y False si
    se agota `limite` o si la unidad se queda parada sin estar arrancando.

    Lanza ErrorInfraestructura si la unidad entra en `failed`: eso no es una
    espera que no termina, es un servicio que ya ha dicho que no va a arrancar,
    y tratarlo como "sigue esperando" es lo que hacia que un fallo de arranque
    costara el limite entero antes de reportarse.

    `sh` y `abrir` se inyectan para poder probar sin systemd ni red, y para que
    quien ya tenga sus propias envolturas (bench-ubatch.py) siga usandolas.
    """
    sh = sh or _sh
    abrir = abrir or _abrir
    t0 = time.time()
    visto_activo = False
    visto_health = False
    ultimo_modelos: list[str] = []
    while time.time() - t0 < limite:
        estado = sh(f"{SYSTEMCTL} is-active {unidad}")
        if estado == "failed":
            raise ErrorInfraestructura(
                f"la unidad {unidad} esta en 'failed': no va a arrancar sola "
                f"(revisa: journalctl -u {unidad} -n 50)")
        if estado == "active":
            visto_activo = True
            try:
                with abrir(f"http://127.0.0.1:{puerto}/health", 5, {}) as r:
                    salud_ok = getattr(r, "status", 200) == 200
            except Exception:
                salud_ok = False
            if salud_ok:
                visto_health = True
                if not modelo:
                    return True
                ultimo_modelos = modelos_servidos(puerto, clave, abrir=abrir)
                if modelo in ultimo_modelos:
                    return True
        elif estado in ("inactive", "deactivating", "unknown", ""):
            arrancando = sh(
                f"{SYSTEMCTL} show -p ActiveState --value {unidad}") == "activating"
            if not arrancando:
                traza(f"    [!] {unidad} en estado {estado!r}, dejo de esperar")
                return False
        time.sleep(pausa)

    if not visto_activo:
        traza(f"    [!] {unidad} no llego a 'active' en {limite} s")
    elif not visto_health:
        traza(f"    [!] {unidad} activa pero /health no respondio 200 en {limite} s")
    else:
        traza(f"    [!] {unidad} sirve, pero no el modelo {modelo!r}: "
              f"{ultimo_modelos or '<no pude leer /v1/models>'}")
    return False


def espera_proceso(proc, puerto: int, modelo: str | None = None,
                   clave: str | None = None, limite: float = 600,
                   pausa: float = 2, abrir=None, traza=print) -> bool:
    """La misma espera, para un servidor de banco lanzado como proceso hijo.

    Las tres condiciones simultaneas de `espera_servicio`, con la primera
    traducida al unico dueno que hay aqui:

        1. `proc.poll() is None` -- el proceso sigue vivo
        2. `/health` responde 200
        3. `/v1/models` (con la clave) sirve el modelo esperado

    Un proceso que ya ha MUERTO lanza ErrorInfraestructura en el acto, por el
    mismo motivo que una unidad en `failed`: no es "todavia no ha arrancado",
    es un arranque que ya ha fallado, y tratarlo como espera cuesta el limite
    entero antes de reportar lo que se sabia en el primer sondeo. En H-031 eso
    fue real: cuatro brazos de MTP murieron al arrancar y el runner los espero
    uno a uno.

    Sin `modelo` la espera se queda en (1) y (2), igual que la de servicio.
    """
    abrir = abrir or _abrir
    t0 = time.time()
    visto_health = False
    ultimo_modelos: list[str] = []
    while True:
        rc = proc.poll()
        if rc is not None:
            raise ErrorInfraestructura(
                f"el servidor de banco (pid {getattr(proc, 'pid', '?')}) murio "
                f"con codigo {rc} antes de servir: no es una espera pendiente, "
                "es un arranque fallido")
        try:
            with abrir(f"http://127.0.0.1:{puerto}/health", 5, {}) as r:
                salud_ok = getattr(r, "status", 200) == 200
        except Exception:
            salud_ok = False
        if salud_ok:
            visto_health = True
            if not modelo:
                return True
            ultimo_modelos = modelos_servidos(puerto, clave, abrir=abrir)
            if modelo in ultimo_modelos:
                return True
        if time.time() - t0 >= limite:
            break
        time.sleep(pausa)

    if not visto_health:
        traza(f"    [!] el banco del puerto {puerto} sigue vivo pero /health no "
              f"respondio 200 en {limite} s")
    else:
        traza(f"    [!] el banco del puerto {puerto} sirve, pero no el modelo "
              f"{modelo!r}: {ultimo_modelos or '<no pude leer /v1/models>'}")
    return False
