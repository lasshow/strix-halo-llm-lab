#!/usr/bin/env python3
"""H-049: pruebas de aptitud para PRODUCCION de un servidor OpenAI-compatible.

Independiente del motor (se ejecuta igual contra llama.cpp y contra Gufo):

  fugas        8 conversaciones, cada una con su clave unica, primero en serie y
               luego todas a la vez. Cuenta: respuestas que contienen SU clave,
               respuestas que contienen la clave de OTRA (fuga), y respuestas
               identicas a las de la serie (greedy).
  concurrencia misma tarea de codigo con 1, 2 y 4 peticiones simultaneas:
               tokens generados totales / tiempo de pared = t/s agregados.
  vision       dos imagenes PNG de un solo color (rojo y azul) generadas aqui;
               se pide el color en una palabra.
  herramientas una funcion get_weather(city); la respuesta debe ser una
               tool_call con ese nombre y city=Bilbao.

Clave opcional por MOTOR_KEY. Escribe un JSON con todo y un resumen por stdout.
"""
import argparse
import base64
import json
import os
import struct
import threading
import time
import urllib.request
import zlib

CLAVES = ["ALFA-3141", "BRAVO-2718", "CHARLIE-1618", "DELTA-1414",
          "ECO-1732", "FOXTROT-2236", "GOLF-2645", "HOTEL-3316"]


def post(base, key, body, timeout=900):
    cab = {"Content-Type": "application/json"}
    if key:
        cab["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=cab)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def cuerpo(model, messages, max_tokens=200, **extra):
    b = {"model": model, "messages": messages, "max_tokens": max_tokens,
         "temperature": 0, "top_k": 1, "top_p": 1, "seed": 1234,
         "chat_template_kwargs": {"enable_thinking": False}}
    b.update(extra)
    return b


def texto(r):
    return (r["choices"][0]["message"].get("content") or "").strip()


def en_paralelo(fns):
    res = [None] * len(fns)

    def corre(i, f):
        try:
            res[i] = f()
        except Exception as e:
            res[i] = e
    hs = [threading.Thread(target=corre, args=(i, f)) for i, f in enumerate(fns)]
    t0 = time.monotonic()
    for h in hs:
        h.start()
    for h in hs:
        h.join()
    return res, time.monotonic() - t0


def prueba_fugas(base, key, model):
    def conv(clave):
        msgs = [
            {"role": "user", "content": "Apunta esta clave de inventario, la "
             "necesitare luego: %s. Contesta solo 'apuntado'." % clave},
            {"role": "assistant", "content": "apuntado"},
            {"role": "user", "content": "Escribe un parrafo de cuatro frases "
             "sobre el mantenimiento de un horno de templado y termina "
             "repitiendo exactamente la clave de inventario que te di."},
        ]
        return cuerpo(model, msgs, max_tokens=220)

    serie = [texto(post(base, key, conv(c))) for c in CLAVES]
    res, _ = en_paralelo([lambda c=c: texto(post(base, key, conv(c))) for c in CLAVES])
    filas = []
    for i, c in enumerate(CLAVES):
        r = res[i]
        if isinstance(r, Exception):
            filas.append({"clave": c, "error": str(r)})
            continue
        otras = [o for o in CLAVES if o != c and o in r]
        filas.append({"clave": c, "propia": c in r, "fugas": otras,
                      "igual_que_serie": r == serie[i], "respuesta": r,
                      "serie": serie[i]})
    return {
        "propias_en_paralelo": sum(1 for f in filas if f.get("propia")),
        "propias_en_serie": sum(1 for i, c in enumerate(CLAVES) if c in serie[i]),
        "fugas": sum(len(f.get("fugas", [])) for f in filas),
        "identicas_a_serie": sum(1 for f in filas if f.get("igual_que_serie")),
        "errores": sum(1 for f in filas if "error" in f),
        "n": len(CLAVES), "filas": filas,
    }


def prueba_concurrencia(base, key, model):
    tarea = ("Escribe en Python una clase ColaPrioridad con push, pop y peek "
             "sobre heapq, con docstrings y tres asserts de prueba al final.")
    out = {}
    for n in (1, 2, 4):
        b = cuerpo(model, [{"role": "user", "content": tarea}], max_tokens=400)
        res, wall = en_paralelo([lambda: post(base, key, b) for _ in range(n)])
        toks = sum(r["usage"]["completion_tokens"] for r in res
                   if not isinstance(r, Exception))
        errores = sum(1 for r in res if isinstance(r, Exception))
        out[str(n)] = {"tokens": toks, "wall_s": round(wall, 2),
                       "agregado_tps": round(toks / wall, 2), "errores": errores}
    return out


def png_color(rgb, lado=96):
    fila = b"\x00" + bytes(rgb) * lado
    crudo = fila * lado

    def trozo(tipo, datos):
        c = struct.pack(">I", len(datos)) + tipo + datos
        return c + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + trozo(b"IHDR", struct.pack(">IIBBBBB", lado, lado, 8, 2, 0, 0, 0))
            + trozo(b"IDAT", zlib.compress(crudo)) + trozo(b"IEND", b""))


def prueba_vision(base, key, model):
    out = {}
    for nombre, rgb, esperado in (("rojo", (220, 20, 20), "rojo"),
                                  ("azul", (20, 40, 220), "azul")):
        url = "data:image/png;base64," + base64.b64encode(png_color(rgb)).decode()
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": "De que color es esta imagen? Responde con una sola palabra en castellano."}]}]
        try:
            r = texto(post(base, key, cuerpo(model, msgs, max_tokens=20)))
            out[nombre] = {"ok": esperado in r.lower(), "respuesta": r}
        except Exception as e:
            out[nombre] = {"ok": False, "error": str(e)}
    return out


def prueba_herramientas(base, key, model):
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Tiempo actual en una ciudad",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "Nombre de la ciudad"}},
            "required": ["city"]}}}]
    msgs = [{"role": "user", "content": "Que tiempo hace ahora en Bilbao?"}]
    try:
        r = post(base, key, cuerpo(model, msgs, max_tokens=200, tools=tools,
                                   tool_choice="auto"))
        m = r["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        ok = False
        if calls:
            f = calls[0]["function"]
            args = json.loads(f.get("arguments") or "{}")
            ok = f.get("name") == "get_weather" and "bilbao" in str(args.get("city", "")).lower()
        return {"ok": ok, "tool_calls": calls, "content": m.get("content"),
                "finish_reason": r["choices"][0].get("finish_reason")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--etiqueta", required=True)
    p.add_argument("--salida", required=True)
    p.add_argument("--solo", default="fugas,concurrencia,vision,herramientas")
    a = p.parse_args()
    key = os.environ.get("MOTOR_KEY") or None
    res = {"motor": a.etiqueta}
    for nombre in a.solo.split(","):
        t0 = time.monotonic()
        res[nombre] = globals()["prueba_" + nombre](a.base, key, a.model)
        res[nombre + "_s"] = round(time.monotonic() - t0, 1)
        r = dict(res[nombre]) if isinstance(res[nombre], dict) else res[nombre]
        if isinstance(r, dict):
            r.pop("filas", None)
        print("[%s] %s: %s" % (a.etiqueta, nombre, json.dumps(r, ensure_ascii=False)[:600]), flush=True)
    with open(a.salida, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
