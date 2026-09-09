#!/usr/bin/env python3
"""
Barrido de ubatch contra un llama-server REAL, con prompt realista.

Por que no usar llama-bench: medido con un modelo distinto al que sirves,
da curvas invertidas y recomendaciones opuestas (ver docs/hallazgos.md H-005).

Uso:
    export LLAMA_API_KEY=...
    ./bench-ubatch.py --url http://localhost:8080 \
        --unit llama-server.service \
        --ubatch 512 1024 2048 4096 \
        --prompt-tokens 33000 --passes 3

Requiere permiso para reiniciar el servicio (systemctl) si se pasa --unit.
Sin --unit, mide solo la configuracion actualmente cargada.
"""
import argparse, json, os, statistics, subprocess, sys, time, urllib.request

def http_post(url, key, payload, timeout=1800):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def wait_ready(url, key, timeout=600):
    """Espera a que el servidor vuelva a responder tras un reinicio."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(
                f"{url}/v1/models", headers={"Authorization": f"Bearer {key}"})
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except Exception:
            time.sleep(5)
    return False

def make_prompt(n_tokens):
    """Texto sintetico de longitud aproximada. ~0.75 tokens por palabra."""
    words = ("El sistema de inferencia procesa secuencias extensas de texto "
             "para evaluar el rendimiento sostenido de la memoria unificada ")
    unit = len(words.split())
    reps = int(n_tokens / (unit * 0.75)) + 1
    return words * reps

def set_ubatch(unit, ub):
    """Reescribe --batch-size/--ubatch-size en la unidad systemd."""
    path = f"/etc/systemd/system/{unit}"
    with open(path) as f:
        src = f.read()
    import re
    src = re.sub(r"--batch-size \d+", f"--batch-size {ub}", src)
    src = re.sub(r"--ubatch-size \d+", f"--ubatch-size {ub}", src)
    with open(path, "w") as f:
        f.write(src)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", unit], check=True)

def measure(url, key, prompt, model):
    r = http_post(f"{url}/v1/chat/completions", key, {
        "model": model,
        "messages": [{"role": "user", "content": prompt},
                     {"role": "user", "content": "Responde solo: OK"}],
        "max_tokens": 128, "temperature": 0, "cache_prompt": False,
    })
    t = r.get("timings") or r.get("usage", {}).get("timings", {})
    return {
        "prompt_n": t.get("prompt_n"),
        "pp": t.get("prompt_per_second"),
        "tg": t.get("predicted_per_second"),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--model", default="")
    ap.add_argument("--unit", default=None, help="unidad systemd a reiniciar")
    ap.add_argument("--ubatch", nargs="+", type=int, default=[512, 1024, 2048, 4096])
    ap.add_argument("--prompt-tokens", type=int, default=33000)
    ap.add_argument("--passes", type=int, default=3)
    a = ap.parse_args()

    key = os.environ.get("LLAMA_API_KEY", "")
    if not key:
        sys.exit("Falta LLAMA_API_KEY en el entorno")

    prompt = make_prompt(a.prompt_tokens)
    rows = []

    for ub in a.ubatch:
        if a.unit:
            print(f"[*] ubatch={ub}: reconfigurando y reiniciando {a.unit}...")
            set_ubatch(a.unit, ub)
            if not wait_ready(a.url, key):
                sys.exit(f"El servidor no volvio tras poner ubatch={ub}")
        pps, tgs, n = [], [], None
        for i in range(a.passes + 1):        # la 1a pasada se descarta (paginas frias)
            m = measure(a.url, key, prompt, a.model)
            n = m["prompt_n"]
            print(f"    pasada {i}: prompt_n={n} pp={m['pp']:.1f} tg={m['tg']:.2f}"
                  + ("  (descartada)" if i == 0 else ""))
            if i > 0:
                pps.append(m["pp"]); tgs.append(m["tg"])
        rows.append((ub, n, statistics.median(pps), statistics.median(tgs)))

    print("\n| ubatch | prompt_n | pp t/s | tg t/s |")
    print("|---|---|---|---|")
    for ub, n, pp, tg in rows:
        print(f"| {ub} | {n} | {pp:.1f} | {tg:.2f} |")
    best = max(rows, key=lambda r: r[2])
    print(f"\nMejor prefill: ubatch={best[0]} ({best[2]:.1f} t/s)")

if __name__ == "__main__":
    main()
