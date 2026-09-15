#!/usr/bin/env python3
"""H-038: banco de CARGA REAL para decidir si el MTP (draft-mtp) compensa.

Por que existe, si ya hay bench-context.py:

  bench-context.py construye el prompt repitiendo UNA frase castellana miles de
  veces. Eso mide degradacion por longitud de KV cache, que es para lo que se
  escribio, pero es el peor caso imaginable para un borrador especulativo: el
  texto es tan uniforme que la cabeza MTP propone tokens que el modelo objetivo
  rechaza casi siempre, y solo se paga la verificacion. Medir el MTP ahi y
  concluir "el MTP cuesta un 40%" es un error de metodo: se esta midiendo el
  relleno, no la carga de trabajo.

  Aqui los prompts son FICHEROS REALES de este repositorio (codigo Python y
  prosa tecnica en castellano) y las tareas son las que se le piden de verdad
  al servidor: explicar codigo, proponer un cambio, resumir, extraer JSON.

Regla del laboratorio H-004 (una sola variable): este script NO toca el
servidor. Se ejecuta dos veces, contra dos servidores que difieren unicamente
en los flags de especulacion, y se comparan los dos JSONL resultantes con
--comparar.

Uso:
    export LLAMA_API_KEY=...
    ./bench-real.py --url http://HOST:8080 --brazo mtp     --pasadas 2
    ./bench-real.py --url http://HOST:8080 --brazo sin-mtp --pasadas 2
    ./bench-real.py --comparar benchmarks/real-mtp-*.jsonl benchmarks/real-sin-mtp-*.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validacion import (ErrorInfraestructura, cuerpo_json,  # noqa: E402
                        generacion_medida)

RAIZ = Path(__file__).resolve().parent.parent

# Corpus: ficheros reales del repo. Se eligen por tamano y naturaleza distintos
# para que la conclusion no dependa de un solo tipo de entrada.
TAREAS = [
    dict(id="codigo-explica", fichero="scripts/validacion.py", clase="codigo",
         instruccion="Explica que hace este modulo y por que separa las dos "
                     "excepciones. Se concreto y cita nombres de funciones."),
    dict(id="codigo-cambio", fichero="scripts/bench-context.py", clase="codigo",
         instruccion="Propon un cambio concreto para medir tambien la latencia "
                     "del primer token. Escribe el codigo Python del cambio."),
    dict(id="codigo-largo", fichero="scripts/campana.py", clase="codigo",
         instruccion="Resume el flujo de control de este script en pasos "
                     "numerados, citando las funciones implicadas."),
    dict(id="prosa-resume", fichero="README.md", clase="prosa",
         instruccion="Resume este documento en diez frases en castellano, sin "
                     "listas ni titulos."),
    dict(id="prosa-hallazgos", fichero="docs/hallazgos.md", clase="prosa",
         instruccion="Redacta en castellano llano, en un solo parrafo largo, "
                     "que se ha aprendido de estos hallazgos."),
    dict(id="json-extrae", fichero="scripts/bench-ubatch.py", clase="json",
         instruccion="Devuelve SOLO un objeto JSON valido con las claves "
                     "\"funciones\" (lista de nombres de funciones definidas) y "
                     "\"argumentos_cli\" (lista de flags que acepta). Sin "
                     "explicaciones ni bloque de codigo."),
]


def http_post(url: str, key: str, payload: dict, timeout: int = 1800) -> bytes:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ErrorInfraestructura(f"HTTP {r.status}")
        return r.read()


def modelo_servido(url: str, key: str) -> str:
    req = urllib.request.Request(
        f"{url}/v1/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    return d["data"][0]["id"]


def construye_prompt(t: dict) -> str:
    ruta = RAIZ / t["fichero"]
    cuerpo = ruta.read_text(encoding="utf-8", errors="replace")
    return (f"A continuacion va el contenido del fichero {t['fichero']}.\n\n"
            f"```\n{cuerpo}\n```\n\n{t['instruccion']}")


def mide(url: str, key: str, modelo: str, prompt: str, max_tokens: int) -> dict:
    payload = {
        "model": modelo,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "top_k": 1,
        # Sin esto Flash-Next delibera hasta agotar el presupuesto y devuelve
        # content vacio (H-019). Un bench que mide eso no mide trabajo util.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t0 = time.monotonic()
    d = cuerpo_json(http_post(f"{url.rstrip('/')}/v1/chat/completions", key, payload))
    wall = time.monotonic() - t0
    m = generacion_medida(d, max_tokens)
    m["wall_s"] = round(wall, 2)
    return m


def ejecuta(args) -> Path:
    key = os.environ.get("LLAMA_API_KEY")
    if not key:
        sys.exit("falta LLAMA_API_KEY en el entorno")
    modelo = modelo_servido(args.url, key)
    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    salida = RAIZ / "benchmarks" / f"real-{args.brazo}-{sello}.jsonl"
    print(f"[i] brazo={args.brazo} modelo={modelo} pasadas={args.pasadas}")
    print(f"[i] registro: {salida}")

    with salida.open("w") as fh:
        for t in TAREAS:
            prompt = construye_prompt(t)
            # Calentamiento descartado: la primera pasada paga cache frio.
            try:
                c = mide(args.url, key, modelo, prompt, args.max_tokens)
                print(f"[*] {t['id']} calentamiento prompt_n={c['prompt_n']} "
                      f"pp={c['pp']:.1f} tg={c['tg']:.2f}")
            except Exception as e:
                print(f"[X] {t['id']} calentamiento fallo: {e}")
                continue
            # El servidor corre con --cache-reuse: tras el calentamiento el
            # prompt queda cacheado y las pasadas medidas reportan prompt_n=4
            # (solo los tokens nuevos). Por eso su 'pp' NO es prefill real y no
            # debe compararse; el tamano real del prompt es el del calentamiento
            # y se arrastra aparte para poder situar cada medida en su contexto.
            prompt_n_real = c["prompt_n"]
            pp_real = c["pp"]
            for p in range(1, args.pasadas + 1):
                try:
                    m = mide(args.url, key, modelo, prompt, args.max_tokens)
                except Exception as e:
                    print(f"[X] {t['id']} pasada {p} fallo: {e}")
                    continue
                fila = dict(brazo=args.brazo, tarea=t["id"], clase=t["clase"],
                            fichero=t["fichero"], pasada=p, modelo=modelo,
                            prompt_n=m["prompt_n"], pp=m["pp"], tg=m["tg"],
                            prompt_n_real=prompt_n_real, pp_real=pp_real,
                            predicted_n=m["predicted_n"], wall_s=m["wall_s"],
                            finish_reason=m["finish_reason"],
                            truncada=m["truncada"], texto=m["texto"])
                fh.write(json.dumps(fila, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"    pasada {p}: prompt_n={m['prompt_n']} "
                      f"pp={m['pp']:.1f} tg={m['tg']:.2f} "
                      f"gen={m['predicted_n']} wall={m['wall_s']}s")
    return salida


def carga(ruta: str) -> dict:
    por_tarea: dict[str, list[dict]] = {}
    with open(ruta) as fh:
        for linea in fh:
            f = json.loads(linea)
            por_tarea.setdefault(f["tarea"], []).append(f)
    return por_tarea


def compara(ruta_a: str, ruta_b: str) -> None:
    """ruta_a = brazo con MTP, ruta_b = brazo sin MTP."""
    a, b = carga(ruta_a), carga(ruta_b)
    comunes = [t["id"] for t in TAREAS if t["id"] in a and t["id"] in b]
    print(f"\n{'tarea':<18}{'clase':<8}{'prompt':>8}"
          f"{'tg MTP':>9}{'tg sin':>9}{'delta':>9}")
    print("-" * 61)
    deltas: dict[str, list[float]] = {}
    for tid in comunes:
        fa, fb = a[tid], b[tid]
        tga = statistics.median(x["tg"] for x in fa)
        tgb = statistics.median(x["tg"] for x in fb)
        d = (tga / tgb - 1.0) * 100.0
        clase = fa[0]["clase"]
        deltas.setdefault(clase, []).append(d)
        deltas.setdefault("TOTAL", []).append(d)
        pn = fa[0].get("prompt_n_real", fa[0]["prompt_n"])
        print(f"{tid:<18}{clase:<8}{pn:>8}"
              f"{tga:>9.2f}{tgb:>9.2f}{d:>8.1f}%")
    print("-" * 61)
    for clase, ds in deltas.items():
        signo = "a favor del MTP" if statistics.median(ds) > 0 else "en contra del MTP"
        print(f"{clase:<18} mediana {statistics.median(ds):+.1f}%  ({signo})")
    print("\nNota: delta = (tg con MTP / tg sin MTP - 1). Positivo = el MTP acelera.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url")
    p.add_argument("--brazo", choices=["mtp", "sin-mtp"])
    p.add_argument("--pasadas", type=int, default=2)
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--comparar", nargs=2, metavar=("MTP", "SIN_MTP"))
    args = p.parse_args()
    if args.comparar:
        compara(*args.comparar)
        return
    if not args.url or not args.brazo:
        p.error("hacen falta --url y --brazo (o --comparar)")
    ejecuta(args)


if __name__ == "__main__":
    main()
