#!/usr/bin/env python3
"""Corpus de prompts medidos con el tokenizador REAL del servidor y congelados.

Por que existe (auditoria externa de la campana H-031, fallo F):

  El relleno se generaba con una constante, `TOKENS_POR_PALABRA = 1.78`, y se
  etiquetaba el punto con el objetivo pedido. Al pedir 3.000 tokens llegaron
  2.294: un **24 % corto**. No invalidaba la comparacion -- todos los brazos
  recibian el mismo texto --, pero las etiquetas "3k/24k" de las tablas eran
  falsas, y una constante calibrada contra otro tokenizador es folclore en
  cuanto se cambia de modelo.

  Aqui el tamano no se estima: se MIDE contra el servidor, se ajusta hasta
  caer dentro de +-64 tokens del objetivo, y el texto resultante se CONGELA en
  disco con su sha256. A partir de ahi el corpus es un fichero versionado, no
  un texto que se vuelve a generar en cada ejecucion.

Las dos reglas que hacen falta para que un A/B signifique algo:

  1. **El mismo fichero para todos los brazos.** Generar el texto por brazo
     admite que el brazo A y el brazo B midan prompts distintos. `carga()`
     devuelve siempre el mismo fichero para un objetivo dado, y su sha256.
  2. **Se verifica ANTES de medir.** El runner comprueba que el `prompt_n` que
     devuelve el servidor para ese corpus sigue cayendo dentro de la
     tolerancia. Si el tokenizador cambio (otro modelo, otra build), el corpus
     de 8.192 puede valer ahora 9.400 y el punto deja de ser el que dice ser:
     eso ABORTA, no se anota al margen.

Como se mide, en este orden:
  - `POST /tokenize` con `add_special`, si el servidor lo expone: es barato y
    permite iterar. Ojo: no aplica la plantilla de chat, asi que su recuento
    es una aproximacion buena pero no el numero final.
  - `POST /v1/chat/completions` con `max_tokens=1` leyendo `timings.prompt_n`:
    ese SI es el numero que vera el banco, porque va por el mismo camino. Se
    usa siempre para el valor que se congela y para la verificacion.

Uso:
    LLAMA_API_KEY=... python3 prompts.py generar --url http://localhost:8080
    python3 prompts.py verificar          # sha256 de los ficheros vs MANIFEST
    python3 prompts.py listar
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validacion import ErrorInfraestructura, cuerpo_json, timings  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_CORPUS = os.path.join(RAIZ, "benchmarks", "corpus")
MANIFIESTO = "MANIFEST.json"

# Objetivos del plan de pruebas. No son redondos por gusto: son las potencias
# de dos que recorren la ventana util medida (hasta 98k, H-012/H-017).
OBJETIVOS = (2048, 8192, 16384, 32768, 65536)
TOLERANCIA = 64


class ErrorCorpus(Exception):
    """El corpus no es el que dice ser: tamano fuera de tolerancia, fichero
    ausente o sha256 que no cuadra. Aborta el punto; no se apunta al margen."""


# --------------------------------------------------------------- texto base
# Frases de longitud dispar y con cifras, para no darle al tokenizador un
# unico patron que se repite: un texto degenerado tokeniza distinto que prosa
# y el corpus dejaria de parecerse al caso de uso.
_FRASES = (
    "El sistema de control del horno registra la temperatura de cada zona cada quince segundos.",
    "La memoria unificada reparte el mismo pool entre el procesador y la grafica integrada.",
    "El operario anota la desviacion observada en la zona {n} y firma el parte de turno.",
    "Durante el ciclo {n} la resistencia superior consumio 4,7 kW y la inferior 3,9 kW.",
    "El informe compara el rendimiento sostenido con el pico declarado por el fabricante.",
    "Ninguna de las {n} muestras analizadas presento desviaciones fuera del margen admitido.",
    "La ventana de contexto reservada por slot no equivale a la ventana verificada de punta a punta.",
    "Se documenta el procedimiento completo para que otra persona pueda repetirlo sin preguntar.",
)


def texto_de(unidades: int) -> str:
    """Texto determinista de `unidades` frases. La misma entrada, el mismo texto."""
    partes = []
    for i in range(max(1, unidades)):
        partes.append(_FRASES[i % len(_FRASES)].format(n=i + 1))
    partes.append("\n\nResume el texto anterior en una sola frase.")
    return " ".join(partes)


def sha256_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ medidas
def _peticion(url: str, ruta: str, cuerpo: dict, clave: str | None,
              timeout: float = 600) -> dict:
    cab = {"Content-Type": "application/json"}
    if clave:
        cab["Authorization"] = f"Bearer {clave}"
    req = urllib.request.Request(url.rstrip("/") + ruta,
                                 data=json.dumps(cuerpo).encode(), headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return cuerpo_json(r.read())
    except urllib.error.HTTPError as e:
        raise ErrorInfraestructura(f"HTTP {e.code} en {ruta}") from e
    except urllib.error.URLError as e:
        raise ErrorInfraestructura(f"sin respuesta en {ruta}: {e.reason}") from e


def tokens_por_tokenize(url: str, texto: str, clave: str | None = None) -> int | None:
    """Recuento barato via /tokenize. None si el servidor no expone el endpoint."""
    try:
        d = _peticion(url, "/tokenize",
                      {"content": texto, "add_special": True}, clave, timeout=120)
    except ErrorInfraestructura:
        return None
    toks = d.get("tokens")
    if not isinstance(toks, list) or not toks:
        return None
    return len(toks)


def tokens_por_chat(url: str, texto: str, clave: str | None = None,
                    timeout: float = 900) -> int:
    """El recuento que vera el banco: `timings.prompt_n` del camino real.

    Va con `cache_prompt=false` a proposito: con la cache activa el segundo
    sondeo del mismo texto puede no reprocesar el prompt y devolver un
    `prompt_n` que no mide este corpus.
    """
    d = _peticion(url, "/v1/chat/completions", {
        "messages": [{"role": "user", "content": texto}],
        "max_tokens": 1, "temperature": 0, "cache_prompt": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }, clave, timeout=timeout)
    # Aqui solo interesa prompt_n. No se pasa por `timings()` porque ese
    # contrato exige velocidades de generacion plausibles y con max_tokens=1
    # el servidor real devuelve predicted_per_second=0 (visto en el M5): un
    # sondeo de recuento de tokens no es una medida de generacion.
    t = d.get("timings") or {}
    pn = t.get("prompt_n")
    if not isinstance(pn, (int, float)) or int(pn) <= 0:
        raise ErrorInfraestructura(f"timings.prompt_n ausente o no positivo: {pn!r}")
    return int(pn)


def genera(objetivo: int, medidor, tolerancia: int = TOLERANCIA,
           max_iter: int = 24, traza=print) -> tuple[str, int]:
    """Busca el numero de frases cuyo recuento real cae dentro de la tolerancia.

    Ajuste proporcional con bisección de respaldo: el proporcional converge
    rapido y la bisección evita que oscile entre dos valores que se pasan cada
    uno por su lado.
    """
    n = max(1, objetivo // 20)
    lo, hi = 1, None          # lo: se queda corto · hi: se pasa
    mejor = None
    for _ in range(max_iter):
        texto = texto_de(n)
        pn = medidor(texto)
        if mejor is None or abs(pn - objetivo) < abs(mejor[2] - objetivo):
            mejor = (texto, n, pn)
        traza(f"    {n} frases -> prompt_n {pn} (objetivo {objetivo}+-{tolerancia})")
        if abs(pn - objetivo) <= tolerancia:
            return texto, pn
        if pn < objetivo:
            lo = max(lo, n)
        else:
            hi = n if hi is None else min(hi, n)
        propuesto = max(1, round(n * objetivo / pn)) if pn else n * 2
        if hi is not None and not (lo < propuesto < hi):
            propuesto = (lo + hi) // 2
        if propuesto == n:
            propuesto = n + (1 if pn < objetivo else -1)
        if propuesto < 1 or (hi is not None and hi - lo <= 1):
            break
        n = propuesto
    raise ErrorCorpus(
        f"no consegui un corpus de {objetivo}+-{tolerancia} tokens en {max_iter} "
        f"iteraciones; lo mas cerca fue {mejor[2]} con {mejor[1]} frases")


# ------------------------------------------------------------- congelado
def ruta_manifiesto(directorio: str = DIR_CORPUS) -> str:
    return os.path.join(directorio, MANIFIESTO)


def lee_manifiesto(directorio: str = DIR_CORPUS) -> dict:
    ruta = ruta_manifiesto(directorio)
    if not os.path.exists(ruta):
        return {"tolerancia": TOLERANCIA, "corpus": {}}
    with open(ruta, encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("corpus", {})
    d.setdefault("tolerancia", TOLERANCIA)
    return d


def congela(objetivo: int, texto: str, prompt_n: int,
            directorio: str = DIR_CORPUS, modelo: str | None = None,
            medido_con: str = "/v1/chat/completions") -> dict:
    """Escribe el corpus y lo anota en el MANIFEST con su sha256.

    El nombre lleva el hash: `8192-1a2b3c4d.txt`. Si alguien edita el fichero,
    el sha256 del manifiesto deja de cuadrar y `verifica_ficheros()` lo canta.
    """
    os.makedirs(directorio, exist_ok=True)
    sha = sha256_texto(texto)
    nombre = f"{objetivo}-{sha[:8]}.txt"
    with open(os.path.join(directorio, nombre), "w", encoding="utf-8") as f:
        f.write(texto)
    man = lee_manifiesto(directorio)
    entrada = {
        "fichero": nombre,
        "sha256": sha,
        "objetivo": objetivo,
        "prompt_n": prompt_n,
        "tolerancia": TOLERANCIA,
        "medido_con": medido_con,
        "modelo": modelo,
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    man["corpus"][str(objetivo)] = entrada
    with open(ruta_manifiesto(directorio), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    return entrada


def carga(objetivo: int, directorio: str = DIR_CORPUS) -> dict:
    """Devuelve el corpus congelado de ese objetivo: SIEMPRE el mismo fichero.

    Que esto no dependa de nada mas que del objetivo es la garantia de que el
    brazo A y el brazo B miden el mismo texto. Comprueba el sha256 al leer:
    un corpus editado a mano no puede colarse como el que dice el manifiesto.
    """
    man = lee_manifiesto(directorio)
    e = man["corpus"].get(str(objetivo))
    if not e:
        raise ErrorCorpus(
            f"no hay corpus congelado para {objetivo} tokens en {directorio}; "
            f"generalo con: prompts.py generar --objetivos {objetivo}")
    ruta = os.path.join(directorio, e["fichero"])
    if not os.path.exists(ruta):
        raise ErrorCorpus(f"el manifiesto declara {e['fichero']} y no esta en disco")
    with open(ruta, encoding="utf-8") as f:
        texto = f.read()
    sha = sha256_texto(texto)
    if sha != e["sha256"]:
        raise ErrorCorpus(
            f"{e['fichero']} no coincide con su sha256 del manifiesto "
            f"({sha[:12]} vs {e['sha256'][:12]}): el corpus se ha modificado")
    return dict(e, ruta=ruta, texto=texto)


def verifica_prompt_n(objetivo: int, prompt_n: int,
                      tolerancia: int = TOLERANCIA) -> int:
    """El recuento real de ESTA ejecucion contra el objetivo declarado.

    Se llama antes de cada A/B. Si el tokenizador ha cambiado bajo los pies
    (otro modelo, otra build), el corpus de 8.192 puede valer ahora 9.400: el
    punto ya no es el que dice su etiqueta y la comparacion con la linea base
    no vale. Es un ABORTO, no una nota al pie.
    """
    if abs(prompt_n - objetivo) > tolerancia:
        raise ErrorCorpus(
            f"el corpus de {objetivo} tokens mide {prompt_n} en este servidor "
            f"(desvio {prompt_n - objetivo:+d}, tolerancia +-{tolerancia}): "
            "el tokenizador no es el que se uso al congelarlo")
    return prompt_n


def verifica_ficheros(directorio: str = DIR_CORPUS) -> list[str]:
    """sha256 de cada fichero contra el manifiesto. Devuelve la lista de fallos."""
    man = lee_manifiesto(directorio)
    problemas = []
    for objetivo, e in sorted(man["corpus"].items(), key=lambda kv: int(kv[0])):
        try:
            carga(int(objetivo), directorio)
        except ErrorCorpus as err:
            problemas.append(f"{objetivo}: {err}")
    return problemas


# ------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("accion", choices=("generar", "verificar", "listar"))
    ap.add_argument("--url", default=os.environ.get("LLAMA_URL", "http://localhost:8080"))
    ap.add_argument("--dir", default=DIR_CORPUS)
    ap.add_argument("--objetivos", type=int, nargs="+", default=list(OBJETIVOS))
    ap.add_argument("--tolerancia", type=int, default=TOLERANCIA)
    ap.add_argument("--modelo", default=None, help="se anota en el manifiesto")
    ap.add_argument("--rehacer", action="store_true",
                    help="regenera aunque ya exista congelado (cambia el sha)")
    a = ap.parse_args()

    if a.accion == "listar":
        man = lee_manifiesto(a.dir)
        if not man["corpus"]:
            print(f"sin corpus congelado en {a.dir}")
            return 0
        for k, e in sorted(man["corpus"].items(), key=lambda kv: int(kv[0])):
            print(f"{k:>6} -> {e['fichero']}  prompt_n {e['prompt_n']}  "
                  f"sha {e['sha256'][:12]}  {e.get('fecha', '?')}")
        return 0

    if a.accion == "verificar":
        problemas = verifica_ficheros(a.dir)
        for p in problemas:
            print(f"[X] {p}", file=sys.stderr)
        if problemas:
            return 2
        print(f"corpus integro en {a.dir}")
        return 0

    clave = os.environ.get("LLAMA_API_KEY", "")
    if not clave:
        print("Falta LLAMA_API_KEY (el servidor productivo exige clave)",
              file=sys.stderr)
        return 1

    # Iterar por /tokenize si existe (barato) y cerrar SIEMPRE por el camino
    # real: el numero que se congela tiene que ser el que vera el banco.
    usa_tokenize = tokens_por_tokenize(a.url, "prueba", clave) is not None
    print(f"[i] tokenizador: {'/tokenize' if usa_tokenize else '/v1/chat/completions'}"
          f" (el valor congelado siempre sale de timings.prompt_n)")

    for objetivo in a.objetivos:
        man = lee_manifiesto(a.dir)
        if not a.rehacer and str(objetivo) in man["corpus"]:
            print(f"[i] {objetivo}: ya congelado, no lo toco (--rehacer para forzar)")
            continue
        print(f"[*] objetivo {objetivo} tokens")
        try:
            medidor = ((lambda t: tokens_por_tokenize(a.url, t, clave))
                       if usa_tokenize else (lambda t: tokens_por_chat(a.url, t, clave)))
            texto, _ = genera(objetivo, medidor, a.tolerancia)
            real = tokens_por_chat(a.url, texto, clave)
            if abs(real - objetivo) > a.tolerancia:
                # /tokenize se quedo cerca pero la plantilla de chat mueve el
                # recuento: se reajusta ya por el camino real.
                texto, real = genera(objetivo,
                                     lambda t: tokens_por_chat(a.url, t, clave),
                                     a.tolerancia)
            e = congela(objetivo, texto, real, a.dir, modelo=a.modelo)
        except (ErrorCorpus, ErrorInfraestructura) as err:
            print(f"[X] {objetivo}: {err}", file=sys.stderr)
            return 2
        print(f"    congelado {e['fichero']}  prompt_n {e['prompt_n']}  "
              f"sha {e['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
