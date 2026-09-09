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

# Tokens por palabra, MEDIDO en este tokenizador con este texto castellano
# (no estimado): pidiendo 33.000 con el factor teorico 0.75 salieron 58.742
# tokens reales, es decir ~1.78 tokens por palabra. El castellano se
# fragmenta mucho mas que el ingles en tokenizadores entrenados en ingles.
TOKENS_POR_PALABRA = 1.78

def make_prompt(n_tokens):
    """Texto sintetico de longitud aproximada.

    Usa un factor medido, no teorico. Verifica siempre 'prompt_n' en la
    salida: si se desvia mas de un 10% del objetivo, recalibra el factor.
    """
    words = ("El sistema de inferencia procesa secuencias extensas de texto "
             "para evaluar el rendimiento sostenido de la memoria unificada ")
    unit = len(words.split())
    reps = int(n_tokens / (unit * TOKENS_POR_PALABRA)) + 1
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
    # 'restart' directo falla: llama-server no atiende SIGTERM mientras procesa
    # un prefill largo, systemd agota TimeoutStopSec y aborta el proceso a media
    # transicion ("State 'stop-sigterm' timed out. Aborting."). El cliente ve un
    # RemoteDisconnected que parece un fallo de memoria y no lo es.
    # Paramos y esperamos de verdad a que el proceso se haya ido.
    subprocess.run(["systemctl", "stop", unit], check=False)
    for _ in range(60):
        r = subprocess.run(["systemctl", "is-active", "--quiet", unit])
        if r.returncode != 0:
            break
        time.sleep(2)
    else:
        sys.exit(f"{unit} no se detuvo en 120 s; abortando el barrido")
    time.sleep(3)          # margen para que se libere la memoria de la iGPU
    subprocess.run(["systemctl", "start", unit], check=True)

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
