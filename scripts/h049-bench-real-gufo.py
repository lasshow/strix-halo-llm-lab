#!/usr/bin/env python3
"""H-049: el banco de CARGA REAL (bench-real.py) contra Gufo.

bench-real.py lee la velocidad de los `timings` que devuelve llama-server, y
Gufo no los devuelve. Gufo si escribe, por cada peticion, una linea
`event=completed` en su log con prefill_tokens, prefill_tps, generated_tokens y
decode_tps. Este adaptador:

  - IMPORTA de bench-real.py las tareas y la construccion del prompt (mismos
    ficheros, misma instruccion: los prompts son identicos byte a byte);
  - manda el MISMO cuerpo de peticion (greedy, sin razonamiento, 400 tokens);
  - tras cada peticion, lee la ultima linea `event=completed` del log del
    contenedor (las peticiones van de una en una, asi que es la suya);
  - escribe el mismo formato JSONL que bench-real.py, para compararlo con
    `bench-real.py --comparar`.

Uso:
  h049-bench-real-gufo.py --url http://127.0.0.1:8081 --contenedor gufo-h049 \
      --pasadas 2 --salida real-gufo.jsonl
"""
import argparse
import importlib.util as _ilu
import json
import os
import re
import subprocess
import time
import urllib.request

_ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench-real.py")
_spec = _ilu.spec_from_file_location("bench_real", _ruta)
br = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(br)


def ultima_linea(contenedor):
    log = subprocess.run(["podman", "logs", "--tail", "40", contenedor],
                         capture_output=True, text=True).stdout + \
        subprocess.run(["podman", "logs", "--tail", "40", contenedor],
                       capture_output=True, text=True).stderr
    lineas = [l for l in log.splitlines()
              if "event=completed" in l and "/v1/chat/completions" in l]
    if not lineas:
        raise RuntimeError("no hay linea event=completed en el log")
    campos = dict(re.findall(r"(\w+)=([^\s]+)", lineas[-1]))
    return campos


def mide(url, modelo, prompt, max_tokens, contenedor):
    payload = {
        "model": modelo,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "top_k": 1,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    d = json.load(urllib.request.urlopen(req, timeout=1800))
    wall = time.monotonic() - t0
    time.sleep(0.3)  # el log se escribe al cerrar la peticion
    c = ultima_linea(contenedor)
    ch = d["choices"][0]
    return {
        "prompt_n": int(c["prefill_tokens"]),
        "prompt_total": int(c["prompt_tokens"]),
        "pp": float(c["prefill_tps"]),
        "tg": float(c["decode_tps"]),
        "predicted_n": int(c["generated_tokens"]),
        "aceptacion_pct": float(c.get("acceptance_pct", "nan")),
        "ttft_ms": float(c["ttft_ms"]),
        "finish_reason": ch.get("finish_reason") or "stop",
        "texto": (ch["message"].get("content") or "").strip(),
        "wall_s": round(wall, 2),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--contenedor", required=True)
    p.add_argument("--pasadas", type=int, default=2)
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--salida", required=True)
    a = p.parse_args()

    with urllib.request.urlopen(a.url.rstrip("/") + "/v1/models", timeout=60) as r:
        modelo = json.load(r)["data"][0]["id"]
    print("[i] brazo=gufo modelo=%s pasadas=%d" % (modelo, a.pasadas), flush=True)

    with open(a.salida, "w") as fh:
        for t in br.TAREAS:
            prompt = br.construye_prompt(t)
            try:
                c = mide(a.url, modelo, prompt, a.max_tokens, a.contenedor)
                print("[*] %s calentamiento prompt_n=%d pp=%.1f tg=%.2f"
                      % (t["id"], c["prompt_n"], c["pp"], c["tg"]), flush=True)
            except Exception as e:
                print("[X] %s calentamiento fallo: %s" % (t["id"], e), flush=True)
                continue
            for n in range(1, a.pasadas + 1):
                try:
                    m = mide(a.url, modelo, prompt, a.max_tokens, a.contenedor)
                except Exception as e:
                    print("[X] %s pasada %d fallo: %s" % (t["id"], n, e), flush=True)
                    continue
                fila = dict(brazo="gufo", tarea=t["id"], clase=t["clase"],
                            fichero=t["fichero"], pasada=n, modelo=modelo,
                            prompt_n=m["prompt_n"], pp=m["pp"], tg=m["tg"],
                            prompt_n_real=c["prompt_total"], pp_real=c["pp"],
                            predicted_n=m["predicted_n"], wall_s=m["wall_s"],
                            aceptacion_pct=m["aceptacion_pct"],
                            finish_reason=m["finish_reason"],
                            truncada=m["finish_reason"] == "length",
                            texto=m["texto"])
                fh.write(json.dumps(fila, ensure_ascii=False) + "\n")
                fh.flush()
                print("    pasada %d: prompt_n=%d tg=%.2f gen=%d acept=%.1f%% wall=%ss"
                      % (n, m["prompt_n"], m["tg"], m["predicted_n"],
                         m["aceptacion_pct"], m["wall_s"]), flush=True)


if __name__ == "__main__":
    main()
