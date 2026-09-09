#!/usr/bin/env python3
"""
Barrido de LONGITUD DE CONTEXTO contra un llama-server real.

Responde a dos preguntas que el barrido de ubatch no puede responder:

  1. Cuanto se degradan prefill y generacion al crecer el contexto.
     El KV cache crece linealmente y hay que releerlo por token generado,
     asi que 'tg' cae aunque el modelo sea el mismo. Comparar tg medido a
     512 tokens con tg medido a 100k es un error de metodo frecuente.

  2. Hasta donde llega la ventana UTIL de verdad. El servidor anuncia
     262.144 pero eso solo significa que reservo las estructuras. Aqui se
     comprueba empiricamente que responde y que la respuesta es coherente.

A diferencia de bench-ubatch.py, NO reinicia el servicio: la configuracion
es constante y lo unico que cambia es el tamano del prompt. Eso mantiene
una sola variable, que es la regla del laboratorio (ver hallazgos H-004).

Uso:
    export LLAMA_API_KEY=...
    ./bench-context.py --url http://localhost:8080 \
        --tokens 4000 16000 32000 65000 100000 131000 --passes 2
"""
import argparse, json, os, statistics, sys, time, urllib.request

# Medido en este tokenizador con este texto castellano, no estimado.
# Verifica siempre prompt_n en la salida y recalibra si se desvia >10%.
TOKENS_POR_PALABRA = 1.78

FRASE = ("El sistema de inferencia procesa secuencias extensas de texto "
         "para evaluar el rendimiento sostenido de la memoria unificada ")


def http_post(url, key, payload, timeout=3600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def make_prompt(n_tokens):
    unit = len(FRASE.split())
    reps = int(n_tokens / (unit * TOKENS_POR_PALABRA)) + 1
    return FRASE * reps


def make_needle_prompt(n_tokens, clave):
    """Mismo relleno pero con un dato enterrado A LA MITAD.

    Un prompt de 100k que devuelve texto no prueba nada: hay que
    comprobar que el modelo LEE lo que hay dentro de la ventana, no
    solo que no se cae. Se entierra en el medio porque los extremos
    son la parte facil (efecto de primacia y recencia).
    """
    relleno = make_prompt(n_tokens)
    mitad = len(relleno) // 2
    marca = f" El codigo de autorizacion del reactor es {clave}. "
    return relleno[:mitad] + marca + relleno[mitad:]


def measure(url, key, prompt, model, max_tokens=64, pregunta=None):
    msgs = [{"role": "user", "content": prompt}]
    msgs.append({"role": "user",
                 "content": pregunta or "Responde solo con la palabra: OK"})
    t0 = time.time()
    r = http_post(f"{url}/v1/chat/completions", key, {
        "model": model, "messages": msgs,
        "max_tokens": max_tokens, "temperature": 0, "cache_prompt": False,
    })
    wall = time.time() - t0
    t = r.get("timings") or r.get("usage", {}).get("timings", {})
    msg = (r.get("choices") or [{}])[0].get("message", {})
    return {
        "prompt_n": t.get("prompt_n"),
        "pp": t.get("prompt_per_second"),
        "tg": t.get("predicted_per_second"),
        "wall": wall,
        "texto": (msg.get("content") or "").strip(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--model", default="")
    ap.add_argument("--tokens", nargs="+", type=int,
                    default=[4000, 16000, 32000, 65000, 100000, 131000])
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--needle", action="store_true",
                    help="ademas del rendimiento, comprueba recuperacion de un dato")
    a = ap.parse_args()

    key = os.environ.get("LLAMA_API_KEY", "")
    if not key:
        sys.exit("Falta LLAMA_API_KEY en el entorno")

    rows = []
    for n in a.tokens:
        print(f"[*] objetivo {n} tokens", flush=True)
        prompt = make_prompt(n)
        pps, tgs, real, wall = [], [], None, None
        fallo = None
        for i in range(a.passes):
            try:
                m = measure(a.url, key, prompt, a.model)
            except Exception as e:
                fallo = f"{type(e).__name__}: {e}"
                print(f"    pasada {i}: FALLO {fallo}", flush=True)
                break
            real, wall = m["prompt_n"], m["wall"]
            print(f"    pasada {i}: prompt_n={real} pp={m['pp']:.1f} "
                  f"tg={m['tg']:.2f} wall={wall:.1f}s", flush=True)
            pps.append(m["pp"]); tgs.append(m["tg"])

        aguja = ""
        if a.needle and not fallo:
            clave = f"K{n // 1000}X7"
            try:
                mm = measure(a.url, key, make_needle_prompt(n, clave), a.model,
                             max_tokens=512,
                             pregunta=("Dime unicamente el codigo de autorizacion "
                                       "del reactor mencionado en el texto."))
                ok = clave in mm["texto"].upper().replace(" ", "")
                aguja = "OK" if ok else f"FALLA (dijo: {mm['texto'][:40]!r})"
            except Exception as e:
                aguja = f"ERROR {type(e).__name__}"
            print(f"    aguja {clave}: {aguja}", flush=True)

        if fallo:
            rows.append((n, None, None, None, None, fallo, aguja))
        else:
            rows.append((n, real, statistics.median(pps),
                         statistics.median(tgs), wall, "", aguja))

    print("\n| objetivo | prompt_n | pp t/s | tg t/s | latencia | aguja |")
    print("|---|---|---|---|---|---|")
    for n, real, pp, tg, wall, fallo, aguja in rows:
        if fallo:
            print(f"| {n} | - | FALLO | - | - | {fallo[:40]} |")
        else:
            print(f"| {n} | {real} | {pp:.1f} | {tg:.2f} | {wall:.1f}s | {aguja} |")

    ok = [r for r in rows if not r[5]]
    if len(ok) >= 2:
        p, u = ok[0], ok[-1]
        print(f"\nDe {p[1]} a {u[1]} tokens: prefill {p[2]:.1f} -> {u[2]:.1f} t/s "
              f"({(u[2]/p[2]-1)*100:+.0f}%), generacion {p[3]:.2f} -> {u[3]:.2f} t/s "
              f"({(u[3]/p[3]-1)*100:+.0f}%)")


if __name__ == "__main__":
    main()
