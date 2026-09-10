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
import argparse, json, os, statistics, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validacion import (ErrorInfraestructura, FalloContrato,  # noqa: E402
                        cuerpo_json, generacion_medida)

# Medido en este tokenizador con este texto castellano, no estimado.
# Verifica siempre prompt_n en la salida y recalibra si se desvia >10%.
TOKENS_POR_PALABRA = 1.78

FRASE = ("El sistema de inferencia procesa secuencias extensas de texto "
         "para evaluar el rendimiento sostenido de la memoria unificada ")


def http_post(url, key, payload, timeout=3600):
    """Devuelve el cuerpo CRUDO. El parseo lo hace validacion.cuerpo_json,
    para que un cuerpo no-JSON (proxy, 502 en html) sea un error tipado."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ErrorInfraestructura(f"HTTP {r.status}")
        return r.read()


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


def measure(url, key, prompt, model, max_tokens=64, pregunta=None, jsonl=None,
            objetivo=None, warmup=False):
    """Una medida validada.

    H-024: antes se hacia `r.get("timings") or ...` con defaults, asi que una
    respuesta sin metricas producia None silencioso; y no se comprobaba que el
    modelo hubiera respondido nada. Ahora la validacion pasa por
    validacion.generacion_medida, que distingue infraestructura de contrato.
    Se manda enable_thinking:false porque en Flash-Next el razonamiento se come
    el presupuesto y devuelve content vacio (H-019).
    """
    msgs = [{"role": "user", "content": prompt}]
    msgs.append({"role": "user",
                 "content": pregunta or "Responde solo con la palabra: OK"})
    t0 = time.time()
    try:
        bruto = http_post(f"{url}/v1/chat/completions", key, {
            "model": model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": 0, "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        })
    except urllib.error.HTTPError as e:
        raise ErrorInfraestructura(f"HTTP {e.code}: {e.read()[:200]!r}") from e
    except urllib.error.URLError as e:
        raise ErrorInfraestructura(f"sin respuesta: {e.reason}") from e
    m = generacion_medida(cuerpo_json(bruto), max_tokens)
    m["wall"] = time.time() - t0
    if jsonl is not None:
        jsonl.write(json.dumps(dict(m, objetivo=objetivo, warmup=warmup,
                                    ts=datetime.now(timezone.utc).isoformat()),
                               ensure_ascii=False) + "\n")
        jsonl.flush()
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--model", default="")
    ap.add_argument("--tokens", nargs="+", type=int,
                    default=[4000, 16000, 32000, 65000, 100000, 131000])
    ap.add_argument("--passes", type=int, default=3,
                    help="pasadas MEDIDAS por punto; ademas se hace 1 de calentamiento")
    ap.add_argument("--jsonl", default="",
                    help="registro crudo por peticion (por defecto benchmarks/crudo-context-<ts>.jsonl)")
    ap.add_argument("--needle", action="store_true",
                    help="ademas del rendimiento, comprueba recuperacion de un dato")
    a = ap.parse_args()

    key = os.environ.get("LLAMA_API_KEY", "")
    if not key:
        sys.exit("Falta LLAMA_API_KEY en el entorno")

    ruta_jsonl = a.jsonl or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "benchmarks",
        f"crudo-context-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(ruta_jsonl)), exist_ok=True)
    print(f"[i] {a.passes} pasadas medidas + 1 de calentamiento por punto")
    print(f"[i] registro crudo: {os.path.abspath(ruta_jsonl)}\n")

    rows = []
    infra = 0
    with open(ruta_jsonl, "a", encoding="utf-8") as jsonl:
        for n in a.tokens:
            print(f"[*] objetivo {n} tokens", flush=True)
            prompt = make_prompt(n)
            pps, tgs, walls, real = [], [], [], None
            fallo = None
            # Calentamiento: la primera peticion de cada punto paga la reserva
            # del KV cache para ese tamano. Se registra y se descarta (H-024).
            for i in range(a.passes + 1):
                es_warmup = (i == 0)
                etiqueta = "calentamiento" if es_warmup else f"pasada {i}"
                try:
                    m = measure(a.url, key, prompt, a.model, jsonl=jsonl,
                                objetivo=n, warmup=es_warmup)
                except (ErrorInfraestructura, FalloContrato) as e:
                    fallo = f"{type(e).__name__}: {e}"
                    if isinstance(e, ErrorInfraestructura):
                        infra += 1
                    print(f"    {etiqueta}: FALLO {fallo}", flush=True)
                    jsonl.write(json.dumps(
                        {"objetivo": n, "warmup": es_warmup, "fallo": fallo,
                         "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
                    jsonl.flush()
                    break
                print(f"    {etiqueta}: prompt_n={m['prompt_n']} pp={m['pp']:.1f} "
                      f"tg={m['tg']:.2f} wall={m['wall']:.1f}s", flush=True)
                if not es_warmup:
                    real = m["prompt_n"]
                    pps.append(m["pp"]); tgs.append(m["tg"]); walls.append(m["wall"])

            aguja = ""
            if a.needle and not fallo:
                clave = f"K{n // 1000}X7"
                try:
                    mm = measure(a.url, key, make_needle_prompt(n, clave), a.model,
                                 max_tokens=512, jsonl=jsonl, objetivo=n,
                                 pregunta=("Dime unicamente el codigo de autorizacion "
                                           "del reactor mencionado en el texto."))
                    if not mm["texto"]:
                        # respuesta vacia no es "no encontro la aguja": es que
                        # no respondio. Se distingue a proposito.
                        aguja = "SIN RESPUESTA"
                    elif clave in mm["texto"].upper().replace(" ", ""):
                        aguja = "OK"
                    else:
                        aguja = f"FALLA (dijo: {mm['texto'][:40]!r})"
                except (ErrorInfraestructura, FalloContrato) as e:
                    aguja = f"ERROR {type(e).__name__}"
                    infra += isinstance(e, ErrorInfraestructura)
                print(f"    aguja {clave}: {aguja}", flush=True)

            if fallo or not pps:
                rows.append((n, None, None, None, None, fallo or "sin medidas", aguja))
            else:
                # la latencia tambien se agrega: antes se guardaba la wall de la
                # ULTIMA pasada junto a medianas de pp/tg, mezclando estadisticos
                rows.append((n, real, statistics.median(pps),
                             statistics.median(tgs), statistics.median(walls), "", aguja))

    print("\n| objetivo | prompt_n | pp t/s | tg t/s | latencia (mediana) | aguja |")
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
    print(f"\nRegistro crudo: {os.path.abspath(ruta_jsonl)}")

    fallidos = len(rows) - len(ok)
    if fallidos:
        print(f"[!] {fallidos}/{len(rows)} puntos sin medida "
              f"({infra} por infraestructura)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
