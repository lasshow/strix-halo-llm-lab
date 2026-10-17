#!/usr/bin/env python3
"""H-045: repite las celdas DUDOSAS de H-044 con muestreo igualado y N pasadas.

H-044 midio una sola pasada por celda y con el muestreo por defecto de cada
motor (Gufo arranca con temperature=1 top_k=20 top_p=0.95). Una diferencia de
una celda en prompts cortos no se distingue del ruido en esas condiciones.

Aqui se fija el muestreo EXPLICITAMENTE en el cuerpo de la peticion, identico
para los dos motores (greedy: temperature 0, top_k 1, top_p 1, seed fijo), y se
repiten N pasadas por celda. Una celda solo cuenta como diferencia real si el
resultado es ESTABLE en todas las pasadas.

IMPORTANTE: los prompts y los verificadores se IMPORTAN del banco de H-044
(bench-calidad-motores.py). No se reescriben aqui: si se reimplementara el
generador de texto largo, los prompts dejarian de ser comparables con H-044.

Uso:
  h045-celdas-dudosas.py --base http://127.0.0.1:8080/v1 --model qwen3.8-flash-next \
      --etiqueta llamacpp --pasadas 3 --salida /tmp/h045-llamacpp.jsonl
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# El fichero del banco lleva guiones en el nombre, asi que no es importable por
# nombre de modulo: se carga por ruta explicita.
import importlib.util as _ilu

_ruta_banco = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bench-calidad-motores.py")
_spec = _ilu.spec_from_file_location("banco_h044", _ruta_banco)
if _spec is None or _spec.loader is None:
    raise SystemExit("no se puede cargar el banco de H-044: %s" % _ruta_banco)
banco = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(banco)

# Celdas dudosas de H-044: las dos rojas de llama.cpp mas las dos agujas (que son
# la unica senal fuera del ruido y conviene reconfirmar con muestreo igualado).
IDS_DUDOSAS = [
    "instruccion-negativa",
    "razonamiento-trampa",
    "aguja-contexto-medio",
    "aguja-contexto-final",
]

CELDAS = [t for t in banco.PRUEBAS if t["id"] in IDS_DUDOSAS]


def pide(base, model, key, prompt, max_tokens=900, timeout=1800):
    """Muestreo GREEDY explicito e identico en los dos motores."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "top_k": 1,
        "top_p": 1,
        "seed": 1234,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    cab = {"Content-Type": "application/json"}
    if key:
        cab["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=cab)
    t0 = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    dt = time.time() - t0
    msg = r["choices"][0]["message"]
    return msg.get("content") or "", r.get("usage", {}), dt


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--etiqueta", required=True)
    p.add_argument("--salida", required=True)
    p.add_argument("--pasadas", type=int, default=3)
    a = p.parse_args()

    if len(CELDAS) != len(IDS_DUDOSAS):
        print("ERROR: el banco no trae todas las celdas esperadas: %s"
              % [t["id"] for t in CELDAS])
        return 2

    key = os.environ.get("MOTOR_KEY") or None

    resumen = {}
    with open(a.salida, "w") as out:
        for celda in CELDAS:
            prompt = celda["prompt"] if celda.get("prompt") else celda["construye"]()
            oks = []
            for n in range(1, a.pasadas + 1):
                try:
                    texto, usage, dt = pide(a.base, a.model, key, prompt)
                except Exception as e:
                    fila = {"id": celda["id"], "motor": a.etiqueta, "pasada": n,
                            "ok": False, "motivo": "fallo peticion: %s" % e,
                            "segundos": None}
                    out.write(json.dumps(fila, ensure_ascii=False) + "\n")
                    out.flush()
                    print("[FALLO] %-24s p%d  %s" % (celda["id"], n, e))
                    oks.append(False)
                    continue
                ok, motivo = celda["verif"](texto)
                oks.append(ok)
                fila = {
                    "id": celda["id"], "motor": a.etiqueta, "pasada": n,
                    "ok": ok, "motivo": motivo, "segundos": round(dt, 2),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "respuesta": texto,
                }
                out.write(json.dumps(fila, ensure_ascii=False) + "\n")
                out.flush()
                print("[%s] %-24s p%d %6.1fs  %s"
                      % ("OK " if ok else "NO ", celda["id"], n, dt, motivo))
            resumen[celda["id"]] = oks

    print("\n== %s: estabilidad en %d pasadas ==" % (a.etiqueta, a.pasadas))
    for k, oks in resumen.items():
        n_ok = sum(1 for x in oks if x)
        etiqueta = ("ESTABLE OK" if n_ok == len(oks) else
                    "ESTABLE NO" if n_ok == 0 else "INESTABLE")
        print("  %-24s %d/%d  %s" % (k, n_ok, len(oks), etiqueta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
