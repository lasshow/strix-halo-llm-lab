#!/usr/bin/env python3
"""Velocidad comparable entre motores OpenAI-compatibles: lectura de prompt (TTFT) y escritura.
Uso: bench_vel.py --base URL --etiqueta X --salida f.jsonl [--key K] [--model M]
"""
import argparse, glob, json, time, urllib.request, uuid

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True)
ap.add_argument("--etiqueta", required=True)
ap.add_argument("--salida", required=True)
ap.add_argument("--key", default="x")
ap.add_argument("--model", default="qwen3.8-flash-next")
ap.add_argument("--reps", type=int, default=2)
ap.add_argument("--casos", default="prefill-10k,prefill-32k,prefill-100k,codigo-512")
a = ap.parse_args()

# corpus fijo: fuentes python del repo strata, en orden estable
corpus = ""
for f in sorted(glob.glob("/models/strata-lab/strata/**/*.py", recursive=True)):
    try:
        corpus += "\n# ==== %s\n" % f + open(f, errors="ignore").read()
    except Exception:
        pass

def peticion(mensajes, max_tokens):
    body = json.dumps({"model": a.model, "messages": mensajes, "max_tokens": max_tokens,
                       "temperature": 0, "stream": True,
                       "stream_options": {"include_usage": True},
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(a.base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + a.key})
    t0 = time.time(); t1 = None; n = 0; usage = None; texto = ""
    with urllib.request.urlopen(req, timeout=1800) as r:
        for linea in r:
            linea = linea.strip()
            if not linea.startswith(b"data:"):
                continue
            dato = linea[5:].strip()
            if dato == b"[DONE]":
                break
            d = json.loads(dato)
            if d.get("usage"):
                usage = d["usage"]
            for ch in d.get("choices") or []:
                c = (ch.get("delta") or {}).get("content") or (ch.get("delta") or {}).get("reasoning_content")
                if c:
                    if t1 is None:
                        t1 = time.time()
                    n += 1; texto += c
    t2 = time.time()
    pt = (usage or {}).get("prompt_tokens"); ct = (usage or {}).get("completion_tokens") or n
    return {"ttft_s": round(t1 - t0, 2) if t1 else None, "total_s": round(t2 - t0, 2),
            "prompt_tokens": pt, "completion_tokens": ct,
            "pp_tok_s": round(pt / (t1 - t0), 1) if (pt and t1) else None,
            "tg_tok_s": round((ct - 1) / (t2 - t1), 2) if (t1 and ct and ct > 1 and t2 > t1) else None,
            "inicio": texto[:80]}

TODOS = {"prefill-10k": (10000, 200), "prefill-32k": (32000, 200), "prefill-100k": (100000, 200),
         "codigo-512": (0, 512), "prosa-512": (-1, 512)}
casos = [(c, TODOS[c][0], TODOS[c][1]) for c in a.casos.split(",")]

with open(a.salida, "a") as out:
    for nombre, objetivo, mt in casos:
        for rep in range(a.reps):
            nonce = uuid.uuid4().hex
            if objetivo == -1:
                msg = [{"role": "user", "content": "ID %s. Escribe un texto de unas 400 palabras sobre la historia industrial de Bilbao, en castellano, sin listas." % nonce}]
            elif objetivo:
                trozo = corpus[: int(objetivo * 3.2)]
                msg = [{"role": "user", "content": "ID %s\n%s\n\nResume en 5 viñetas qué hace este código." % (nonce, trozo)}]
            else:
                msg = [{"role": "user", "content": "ID %s. Escribe en Python una clase LRUCache con get/put O(1) usando OrderedDict, con docstrings y 5 tests unittest." % nonce}]
            try:
                r = peticion(msg, mt)
            except Exception as e:
                r = {"error": str(e)}
            r.update({"motor": a.etiqueta, "caso": nombre, "rep": rep, "ts": time.strftime("%H:%M:%S")})
            print(json.dumps(r, ensure_ascii=False), flush=True)
            out.write(json.dumps(r, ensure_ascii=False) + "\n"); out.flush()
