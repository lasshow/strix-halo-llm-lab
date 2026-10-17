#!/usr/bin/env python3
"""H-047: llama.cpp frente a Gufo con el MISMO GGUF y una aguja que no dispare negativas.

H-045 dejo dos cabos sueltos: (1) cada motor cargaba una cuantizacion distinta
(UD-IQ4_XS en llama.cpp, UD-Q4_K_XL en Gufo), y (2) la aguja de H-044 pide un
"codigo de autorizacion" y llama.cpp se niega a darlo por seguridad, asi que no
mide recuperacion de contexto.

Aqui se pasan las 12 pruebas de H-044 IMPORTADAS del banco (mismos prompts y
verificadores), mas:
  - aguja-neutra-medio / aguja-neutra-final: mismo relleno y mismas
    profundidades (750 y 1450 de 1500 bloques), pero el dato es una referencia
    de lote, sin nada que suene a secreto, y las dos preguntan por el dato.
Las dos agujas originales se mantienen como CONTROL de la negativa.

Muestreo greedy identico en el cuerpo de la peticion y N pasadas por celda,
igual que H-045.

Uso:
  h047-mismo-gguf.py --base http://127.0.0.1:8080/v1 --model qwen3.8-flash-next \
      --etiqueta llamacpp --pasadas 3 --salida /tmp/h047-llamacpp.jsonl
"""
import argparse
import json
import os
import sys
import time
import urllib.request

import importlib.util as _ilu

_ruta_banco = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bench-calidad-motores.py")
_spec = _ilu.spec_from_file_location("banco_h044", _ruta_banco)
if _spec is None or _spec.loader is None:
    raise SystemExit("no se puede cargar el banco de H-044: %s" % _ruta_banco)
banco = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(banco)

AGUJA_NEUTRA = ("La referencia del lote de ladrillo refractario instalado en la "
                "camara limpia es ZX-4417-QD y la anoto el responsable de pruebas.")
PREGUNTA_NEUTRA = ("\n\nPregunta: cual es la referencia del lote de ladrillo "
                   "refractario instalado en la camara limpia? Responde solo con "
                   "la referencia.")


def _texto_neutro(aguja_en, repeticiones=1500):
    """Mismo relleno que banco._texto_largo; solo cambia la aguja."""
    bloque = ("El sistema de control del horno industrial regula la temperatura "
              "mediante lazos PID en cascada y vigila la concentracion de oxigeno. ")
    partes = [bloque] * repeticiones
    partes.insert(aguja_en, AGUJA_NEUTRA + " ")
    return "".join(partes)


NEUTRAS = [
    {"id": "aguja-neutra-medio", "prompt": None,
     "construye": lambda: _texto_neutro(750) + PREGUNTA_NEUTRA,
     "verif": lambda t: banco.v_contiene(t, ["ZX-4417-QD"])},
    {"id": "aguja-neutra-final", "prompt": None,
     "construye": lambda: _texto_neutro(1450) + PREGUNTA_NEUTRA,
     "verif": lambda t: banco.v_contiene(t, ["ZX-4417-QD"])},
]

CELDAS = list(banco.PRUEBAS) + NEUTRAS


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

    if len(banco.PRUEBAS) != 12:
        print("ERROR: el banco no trae las 12 pruebas de H-044")
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
