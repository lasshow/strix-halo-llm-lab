#!/usr/bin/env python3
"""H-034: medir la cabeza MTP sidecar a np=1 SIN desplegar nada.

Reusa la maquinaria de `campana.py` (Contexto, carga_fase, credencial, parada
y rearranque de produccion con manejo de senales) pero suprime a proposito lo
que H-034 NO debe hacer: no promueve la build, no aplica cambios en la unidad,
no corre gates de despliegue. H-034 vota `adoptar` (la build MTP es segura y
rapida a un slot) pero el despliegue con `np=2` es H-035.

Corre las tres fases de `fases_h034.py` en orden y escribe los votos y los
resumenes en `resultados.json`. La produccion se para al empezar (la GPU no da
para dos servidores) y se rearranca SIEMPRE al terminar, pase lo que pase.

    python3 scripts/h034-medir.py --run-id h034-20260911
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

import campana  # noqa: E402
from campana import Contexto, carga_fase, sh, SYSTEMCTL, SUDO  # noqa: E402
from validacion import ErrorInfraestructura  # noqa: E402

UNIDAD = "llama-flashnext"
MODELO = "qwen3.8-flash-next"
PUERTO_PROD = 8080
PUERTO_BANCO = 8081
DIR_BUILDS = "/models/llama-builds"
# baseline = df03399 + PR #28501 (lo que corre produccion hoy)
# candidato = df03399 + PR #28501 + PR #28243 (MTP) — parche 6e8170fb
BASELINE = "df03399b885831b2a1603b3abb0d8c156808e363+14eebc61"
CANDIDATO = "df03399b885831b2a1603b3abb0d8c156808e363+6e8170fb"
FASES = (
    "fases_h034.py:mtp_np1_velocidad",
    "fases_h034.py:mtp_escalera_contexto",
    "fases_h034.py:mtp_vision",
)
DIR_CORPUS = os.path.join(os.path.dirname(AQUI), "benchmarks", "corpus")


class Interrumpida(Exception):
    pass


def _senal(num, _marco):
    raise Interrumpida(f"senal {num}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--unidad", default=UNIDAD)
    ap.add_argument("--modelo", default=MODELO)
    ap.add_argument("--puerto-prod", type=int, default=PUERTO_PROD)
    ap.add_argument("--puerto-banco", type=int, default=PUERTO_BANCO)
    ap.add_argument("--salida", default=None)
    ap.add_argument("--fase", action="append", default=list(FASES))
    a = ap.parse_args(argv)

    a.salida = a.salida or os.path.join(os.path.dirname(AQUI), "evidencias", a.run_id)
    os.makedirs(a.salida, exist_ok=True)
    registro = os.path.join(a.salida, "medidas.jsonl")

    # credencial del ExecStart efectivo, como en campana.py (fallo A)
    p = subprocess.run(["bash", os.path.join(AQUI, "credencial.sh"), "--crudo",
                        a.unidad], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        print(f"No pude derivar la credencial de {a.unidad}: "
              f"{p.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    clave = p.stdout.strip()

    try:
        fases = [(s, carga_fase(s)) for s in a.fase]
    except RuntimeError as e:
        print(f"Fase ilegible: {e}", file=sys.stderr)
        return 1

    resultados = {"run_id": a.run_id, "unidad": a.unidad, "modelo": a.modelo,
                  "baseline": BASELINE, "candidato": CANDIDATO,
                  "modo": "solo-medida (sin promocion, sin cambios en la unidad)",
                  "inicio": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    args = SimpleNamespace(unidad=a.unidad, modelo=a.modelo,
                           puerto_prod=a.puerto_prod, puerto_banco=a.puerto_banco,
                           salida=a.salida, dir_corpus=DIR_CORPUS,
                           tolerancia_corpus=64)
    builds = {"baseline": os.path.join(DIR_BUILDS, BASELINE),
              "candidato": os.path.join(DIR_BUILDS, CANDIDATO)}
    for cual, ruta in builds.items():
        if not os.path.isdir(ruta):
            print(f"No existe la build {cual} en {ruta}", file=sys.stderr)
            return 1
    ctx = Contexto(args, builds, clave, registro)
    ctx.log("=" * 70)
    ctx.log(f"H-034 (solo-medida) {a.run_id}: inicio")

    estado_inicial = sh(f"{SYSTEMCTL} is-active {a.unidad}")
    resultados["estado_inicial"] = estado_inicial
    ctx.log(f"estado inicial de {a.unidad}: {estado_inicial!r}")

    for sig_ in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig_, _senal)

    parada_por_nosotros = False
    votos, fallos_fase = [], 0
    try:
        if estado_inicial == "active":
            ctx.log(f"paro {a.unidad} (la GPU no da para dos servidores)")
            sh(f"{SUDO} {SYSTEMCTL} stop {a.unidad}".strip())
            parada_por_nosotros = True
            time.sleep(2)

        for spec, fn in fases:
            ctx.log(f"--- fase {spec}")
            try:
                r = fn(ctx) or {}
            except Interrumpida as e:
                fallos_fase += 1
                ctx.nota(f"interrumpida ({e}) en {spec}: paro aqui")
                resultados.setdefault("fases", {})[spec] = {"error": f"interrumpida: {e}"}
                resultados["interrumpida"] = str(e)
                break
            except Exception as e:
                fallos_fase += 1
                ctx.nota(f"fase {spec} fallo: {type(e).__name__}: {e}")
                resultados.setdefault("fases", {})[spec] = {"error": f"{type(e).__name__}: {e}"}
                continue
            nombre = r.get("nombre") or getattr(fn, "__name__", spec)
            resultados.setdefault("fases", {})[nombre] = r.get("resumen", {})
            if r.get("error"):
                fallos_fase += 1
                ctx.nota(f"fase {nombre}: {r['error']}")
            if "adoptar" in r:
                votos.append(bool(r["adoptar"]))
    except Interrumpida as e:
        fallos_fase += 1
        ctx.nota(f"interrumpida ({e}) fuera de fase")
        resultados["interrumpida"] = str(e)
    finally:
        if parada_por_nosotros:
            ctx.log(f"arranco {a.unidad} (estaba activa al empezar)")
            sh(f"{SUDO} {SYSTEMCTL} start {a.unidad}".strip())

    resultados["votos_fases"] = votos
    resultados["adoptan_todas"] = bool(votos) and all(votos)
    resultados["nota"] = ("H-034 no despliega: adoptar aqui solo significa que "
                          "la build MTP es segura y rapida a np=1; el despliegue "
                          "con np=2 es H-035")
    resultados["notas"] = ctx.notas
    resultados["estado_final"] = sh(f"{SYSTEMCTL} is-active {a.unidad}")
    resultados["fin"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ruta = os.path.join(a.salida, "resultados.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(resultados, f, indent=1, ensure_ascii=False)
    ctx.log(f"estado final de {a.unidad}: {resultados['estado_final']!r}")
    ctx.log(f"resultados en {ruta}")
    return 2 if fallos_fase else 0


if __name__ == "__main__":
    sys.exit(main())
