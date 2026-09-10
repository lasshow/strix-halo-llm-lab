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
                        cuerpo_json, generacion_medida, recuperacion_aguja)

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
            objetivo=None, warmup=False, fase=None, clave=None):
    """Una medida validada.

    H-024: antes se hacia `r.get("timings") or ...` con defaults, asi que una
    respuesta sin metricas producia None silencioso; y no se comprobaba que el
    modelo hubiera respondido nada. Ahora la validacion pasa por
    validacion.generacion_medida, que distingue infraestructura de contrato.
    Se manda enable_thinking:false porque en Flash-Next el razonamiento se come
    el presupuesto y devuelve content vacio (H-019).

    `fase` etiqueta el registro ("calentamiento", "medida", "needle") para que
    al recalcular desde el JSONL una peticion de recuperacion no se cuele entre
    las muestras de rendimiento: antes compartian warmup=false y eran
    indistinguibles. Con `clave`, el contrato aplicado es el de recuperacion
    (respuesta comprobable), no el de generacion medida.

    TODO intento se registra, incluidos los fallidos: el registro del intento
    de aguja se perdia cuando reventaba, y el JSONL quedaba con solo las dos
    peticiones anteriores, correctas.
    """
    msgs = [{"role": "user", "content": prompt}]
    msgs.append({"role": "user",
                 "content": pregunta or "Responde solo con la palabra: OK"})
    if fase is None:
        fase = "calentamiento" if warmup else "medida"

    def anota(extra):
        if jsonl is None:
            return
        jsonl.write(json.dumps(dict(extra, objetivo=objetivo, warmup=warmup,
                                    fase=fase, clave_esperada=clave,
                                    ts=datetime.now(timezone.utc).isoformat()),
                               ensure_ascii=False) + "\n")
        jsonl.flush()

    t0 = time.time()
    try:
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
        d = cuerpo_json(bruto)
        if clave is not None:
            m = dict(recuperacion_aguja(d, clave))
            m["veredicto"] = "OK"
            try:
                m.update({k: v for k, v in generacion_medida(d, max_tokens).items()
                          if k not in m})
            except (ErrorInfraestructura, FalloContrato):
                pass  # la aguja se juzga por la respuesta, no por sus timings
        else:
            m = generacion_medida(d, max_tokens)
    except (ErrorInfraestructura, FalloContrato) as e:
        anota({"fallo": f"{type(e).__name__}: {e}", "wall": time.time() - t0,
               "veredicto": "FALLA" if isinstance(e, FalloContrato) else "NO EVALUABLE"})
        raise
    m["wall"] = time.time() - t0
    anota(dict(m, fallo=None))
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
            aguja_fallo = None   # None = no se pidio; str = fallo o imposible
            if a.needle and not fallo:
                clave = f"K{n // 1000}X7"
                try:
                    # La aguja NO pasa por el contrato de generacion medida: es
                    # una tarea de resultado comprobable. Con el contrato de
                    # microbenchmark, una respuesta truncada por presupuesto
                    # pasaba por buena, que es justo el caso en que el modelo no
                    # ha llegado a decir la clave.
                    mm = measure(a.url, key, make_needle_prompt(n, clave), a.model,
                                 max_tokens=512, jsonl=jsonl, objetivo=n,
                                 fase="needle", clave=clave,
                                 pregunta=("Dime unicamente el codigo de autorizacion "
                                           "del reactor mencionado en el texto."))
                    aguja = "OK"
                except FalloContrato as e:
                    aguja = f"FALLA ({str(e)[:60]})"
                    aguja_fallo = f"aguja: {e}"
                except ErrorInfraestructura as e:
                    aguja = f"ERROR {type(e).__name__}"
                    aguja_fallo = f"aguja no evaluable: {e}"
                    infra += 1
                print(f"    aguja {clave}: {aguja}", flush=True)

            if fallo or not pps:
                rows.append((n, None, None, None, None, fallo or "sin medidas",
                             aguja, aguja_fallo))
            else:
                # la latencia tambien se agrega: antes se guardaba la wall de la
                # ULTIMA pasada junto a medianas de pp/tg, mezclando estadisticos
                rows.append((n, real, statistics.median(pps),
                             statistics.median(tgs), statistics.median(walls), "",
                             aguja, aguja_fallo))

    print("\n| objetivo | prompt_n | pp t/s | tg t/s | latencia (mediana) | aguja |")
    print("|---|---|---|---|---|---|")
    for n, real, pp, tg, wall, fallo, aguja, _af in rows:
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

    # El veredicto de la aguja SI decide el codigo de salida. Antes se guardaba
    # en una columna decorativa: con clave incorrecta o con 500 solo en la
    # aguja, el script imprimia FALLA/ERROR y salia 0, asi que --needle no
    # servia como prueba de que el contexto util este validado.
    agujas_mal = [(r[0], r[7]) for r in rows if r[7]]
    if agujas_mal:
        print(f"[!] recuperacion de aguja no superada en {len(agujas_mal)} punto(s):",
              file=sys.stderr)
        for n, det in agujas_mal:
            print(f"    {n}: {det}", file=sys.stderr)

    fallidos = len(rows) - len(ok)
    if fallidos:
        print(f"[!] {fallidos}/{len(rows)} puntos sin medida "
              f"({infra} por infraestructura)", file=sys.stderr)
        return 2
    if agujas_mal:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
