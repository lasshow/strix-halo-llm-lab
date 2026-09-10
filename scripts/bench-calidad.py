#!/usr/bin/env python3
"""Bateria de calidad v2: castellano / ingles / chino + codigo real 2026.

Lanza private/prompts.json contra un llama-server y guarda las respuestas
crudas en private/resp-<etiqueta>.json para correccion manual y para los
verificadores de compilacion (scripts/verifica-codigo.py).

No puntua nada por si mismo: el laboratorio corrige a mano y ejecuta el
codigo. Aqui solo se mide y se registra (pp/tg reales del servidor).
"""
import argparse, json, os, pathlib, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "private" / "prompts.json"
PUBLICA = ROOT / "benchmarks" / "bateria-publica.json"

# Mismos codigos que el resto del instrumental (ver scripts/validacion.py):
SALIDA_MODELO = 2   # el modelo incumplio el contrato
SALIDA_BANCO = 3    # el banco no pudo evaluar (red, servidor, ficheros)


def key(path):
    p = pathlib.Path(os.path.expanduser(path))
    return p.read_text().strip() if p.exists() else ""


def ask(url, api_key, prompt, max_tokens, temp, sin_razonamiento=False):
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False,
    }
    if sin_razonamiento:
        # H-019: en prompts de codigo con requisitos apilados el modelo entra en
        # bucle de deliberacion y agota el presupuesto sin emitir respuesta.
        # Desactivar el pensamiento en la plantilla lo corta (19x mas rapido).
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    wall = time.monotonic() - t0
    ch = d["choices"][0]["message"]
    txt = ch.get("content") or ""
    reas = ch.get("reasoning_content") or ""
    u = d.get("usage", {})
    tim = d.get("timings", {}) or {}
    return {
        "texto": txt,
        "razonamiento_chars": len(reas),
        "vacia": not txt.strip(),
        "prompt_n": u.get("prompt_tokens"),
        "gen_n": u.get("completion_tokens"),
        "pp": tim.get("prompt_per_second"),
        "tg": tim.get("predicted_per_second"),
        "wall_s": round(wall, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://100.95.250.45:8080")
    ap.add_argument("--etiqueta", required=True, help="p.ej. flashnext-v2")
    ap.add_argument("--keyfile", default="~/.secrets/m5-llama-api.key")
    ap.add_argument("--max-tokens", type=int, default=4096,
                    help=">=4096: con 2k Flash-Next gasta el presupuesto en reasoning y devuelve vacio (H-016)")
    ap.add_argument("--temp", type=float, default=0.0)
    ap.add_argument("--bateria", default=None,
                    help="JSON de enunciados. Por defecto private/prompts.json "
                         "si existe, si no benchmarks/bateria-publica.json")
    ap.add_argument("--salida", default=None,
                    help="donde guardar las respuestas (por defecto "
                         "private/resp-<etiqueta>.json)")
    ap.add_argument("--solo", default="", help="ids separados por coma")
    ap.add_argument("--sin-razonamiento", action="store_true",
                    help="enable_thinking=False en la plantilla (ver H-019)")
    a = ap.parse_args()

    ruta = pathlib.Path(a.bateria) if a.bateria else (
        PROMPTS if PROMPTS.exists() else PUBLICA)
    if not ruta.exists():
        print(f"❌ no encuentro la bateria en {ruta} (usa --bateria)", file=sys.stderr)
        return SALIDA_BANCO
    d = json.loads(ruta.read_text())
    prompts = d["prompts"] if isinstance(d, dict) else d
    print(f"[i] bateria: {ruta} ({len(prompts)} casos)")
    if a.solo:
        want = {s.strip() for s in a.solo.split(",")}
        prompts = [p for p in prompts if p["id"] in want]

    k = key(a.keyfile)
    out = pathlib.Path(a.salida) if a.salida else (
        ROOT / "private" / f"resp-{a.etiqueta}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    res = json.loads(out.read_text()) if out.exists() else {}

    for i, p in enumerate(prompts, 1):
        if p["id"] in res:
            print(f"[{i}/{len(prompts)}] {p['id']} ya hecho, salto")
            continue
        print(f"[{i}/{len(prompts)}] {p['id']} ({p['cat']}) ...", end="", flush=True)
        try:
            r = ask(a.url, k, p["p"], a.max_tokens, a.temp, a.sin_razonamiento)
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {e}"}
            print(" ERROR", r["error"])
        else:
            flag = " VACIA" if r["vacia"] else ""
            print(f" {r['gen_n']} tok, pp {r['pp']:.1f} tg {r['tg']:.1f}, {r['wall_s']}s{flag}")
        r["cat"] = p["cat"]
        r["lang"] = p.get("lang")
        res[p["id"]] = r
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1))

    ok = [v for v in res.values() if "error" not in v]
    fallidas = {i: v["error"] for i, v in res.items() if "error" in v}
    vac = [i for i, v in res.items() if v.get("vacia")]
    tgs = [v["tg"] for v in ok if v.get("tg")]
    pps = [v["pp"] for v in ok if v.get("pp")]
    print(f"\n{len(ok)}/{len(res)} respuestas")
    if tgs:
        print(f"tg mediana {sorted(tgs)[len(tgs)//2]:.1f} t/s | pp mediana {sorted(pps)[len(pps)//2]:.1f} t/s")
    print(f"vacias: {vac or 'ninguna'}")
    if fallidas:
        print(f"fallidas: {len(fallidas)} -> " +
              ", ".join(f"{i}: {e[:60]}" for i, e in list(fallidas.items())[:5]))
    print(f"-> {out}")

    # H-025: antes esto salia 0 aunque las N peticiones hubieran fallado con
    # HTTP 500, asi que una campana entera contra un servidor caido se leia
    # como ejecucion correcta. Las fallidas SE CONSERVAN en el JSON (no se
    # borran ni se reintenta hasta cuadrar): solo cambia el codigo de salida.
    if not ok:
        print("❌ ninguna peticion respondio: no hay nada que medir", file=sys.stderr)
        return SALIDA_BANCO
    if fallidas:
        print(f"❌ {len(fallidas)} peticiones fallaron", file=sys.stderr)
        return SALIDA_BANCO
    if vac:
        print(f"❌ {len(vac)} respuestas vacias (contrato incumplido)", file=sys.stderr)
        return SALIDA_MODELO
    return 0


if __name__ == "__main__":
    sys.exit(main())
