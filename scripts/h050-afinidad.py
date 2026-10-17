#!/usr/bin/env python3
"""H-050: coste de alternar conversaciones largas en un servidor de 2 slots.

Es el patron de Hermes: varias conversaciones (agente, delegacion, MoA) contra
el mismo servidor, cada una con un historial largo que crece turno a turno. Con
2 slots y 3 conversaciones, un servidor que solo guarda la cache en el slot
tiene que reprocesar el historial entero cada vez que vuelve una conversacion
desalojada; uno que restaura la cache desde RAM no.

Tres conversaciones con un documento distinto cada una (los tres tercios de
docs/hallazgos.md, ~15k tokens), intercaladas A B C A B C A B C. Cada turno
anade la respuesta anterior y una pregunta nueva. Se mide el tiempo de pared con
max_tokens=48 (domina el prefill) y se guarda el usage que devuelva el servidor.

Uso:
  h050-afinidad.py --base http://127.0.0.1:8080/v1 --model qwen3.8-flash-next \
      --etiqueta llamacpp --doc docs/hallazgos.md --salida h050-llamacpp.jsonl
"""
import argparse
import json
import os
import time
import urllib.request

PREGUNTAS = [
    "Resume en dos frases de que trata este documento.",
    "Cita el identificador de un hallazgo que mencione la memoria.",
    "Cual es la conclusion mas importante? Una frase.",
]


def post(base, key, body):
    cab = {"Content-Type": "application/json"}
    if key:
        cab["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=cab)
    t0 = time.monotonic()
    r = json.load(urllib.request.urlopen(req, timeout=1800))
    return r, time.monotonic() - t0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--etiqueta", required=True)
    p.add_argument("--doc", required=True)
    p.add_argument("--salida", required=True)
    a = p.parse_args()
    key = os.environ.get("MOTOR_KEY") or None

    texto = open(a.doc, encoding="utf-8").read()
    tercio = len(texto) // 3
    docs = [texto[i * tercio:(i + 1) * tercio] for i in range(3)]
    convs = {c: [{"role": "user", "content": "Documento %s:\n\n%s\n\n%s"
                  % (c, docs[i], PREGUNTAS[0])}]
             for i, c in enumerate("ABC")}

    with open(a.salida, "w") as out:
        for ronda in range(3):
            for c in "ABC":
                msgs = convs[c]
                if ronda > 0:
                    msgs.append({"role": "user", "content": PREGUNTAS[ronda]})
                body = {"model": a.model, "messages": msgs, "max_tokens": 48,
                        "temperature": 0, "top_k": 1, "seed": 1234,
                        "chat_template_kwargs": {"enable_thinking": False}}
                r, wall = post(a.base, key, body)
                resp = (r["choices"][0]["message"].get("content") or "").strip()
                msgs.append({"role": "assistant", "content": resp})
                u = r.get("usage", {})
                t = r.get("timings", {})
                fila = {"motor": a.etiqueta, "ronda": ronda + 1, "conv": c,
                        "wall_s": round(wall, 2),
                        "prompt_tokens": u.get("prompt_tokens"),
                        "prompt_n_procesados": t.get("prompt_n"),
                        "respuesta": resp[:200]}
                out.write(json.dumps(fila, ensure_ascii=False) + "\n")
                out.flush()
                print("[%s] ronda %d conv %s  %7.2fs  prompt=%s procesados=%s"
                      % (a.etiqueta, ronda + 1, c, wall, u.get("prompt_tokens"),
                         t.get("prompt_n")), flush=True)


if __name__ == "__main__":
    main()
