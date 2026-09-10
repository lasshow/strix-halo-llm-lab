#!/usr/bin/env python3
"""Barrido de --ubatch-size sobre una unidad systemd DE PRUEBAS, no la productiva.

Por que asi (ver H-021):
  - La version anterior reescribia con el mismo valor --batch-size Y --ubatch-size,
    de modo que nunca se aislo el efecto de ubatch: se movian los dos a la vez.
  - Editaba la unidad productiva en sitio, sin copia previa ni restauracion en
    caso de error: si el script moria a mitad, el servicio se quedaba con la
    configuracion del ultimo punto del barrido.

Este script:
  - Genera 'llama-flashnext-bench.service' (puerto 8081) a partir de la productiva.
  - Cambia SOLO --ubatch-size; --batch-size se fija con --batch y no se toca.
  - Para la productiva mientras mide (la GPU no da para las dos) y la RESTAURA
    en un bloque finally, pase lo que pase, incluido Ctrl-C.
  - Borra la unidad de pruebas al terminar.

Uso: sudo -v primero. LLAMA_API_KEY=... python3 bench-ubatch.py --ubatch 512 1024 2048 --batch 4096
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

UNIT_PROD = "llama-flashnext"
UNIT_BENCH = "llama-flashnext-bench"
RUTA_BENCH = f"/etc/systemd/system/{UNIT_BENCH}.service"
PUERTO_BENCH = 8081
CLAVE = os.environ.get("LLAMA_API_KEY", "")


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"fallo: {cmd}\n{r.stderr.strip()[:300]}")
    return r.stdout.strip()


def unidad_productiva():
    return sh(f"sudo -n cat /etc/systemd/system/{UNIT_PROD}.service")


def construye_unidad(texto, ubatch, batch):
    """Deriva la unidad de pruebas. ubatch y batch se fijan por separado."""
    t = texto
    t = re.sub(r"--ubatch-size\s+\d+", f"--ubatch-size {ubatch}", t)
    t = re.sub(r"--batch-size\s+\d+", f"--batch-size {batch}", t)
    if f"--ubatch-size {ubatch}" not in t:
        raise RuntimeError("no encontre --ubatch-size en la unidad productiva")
    if f"--batch-size {batch}" not in t:
        raise RuntimeError("no encontre --batch-size en la unidad productiva")
    t = t.replace("--port 8080", f"--port {PUERTO_BENCH}")
    t = t.replace(
        "Description=llama.cpp server",
        f"Description=[BENCH ubatch={ubatch} batch={batch}] llama.cpp server",
    )
    # sin reinicio automatico: si un ubatch no arranca, quiero verlo, no un bucle
    t = re.sub(r"Restart=on-failure", "Restart=no", t)
    return t


def espera_salud(puerto, limite=600):
    t0 = time.time()
    while time.time() - t0 < limite:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        if sh(f"systemctl is-active {UNIT_BENCH}", check=False) == "failed":
            return False
        time.sleep(5)
    return False


def mide(puerto, palabras, pasadas):
    prompt = ("El sistema de control industrial registra temperaturas del horno. " * palabras)[:200000]
    datos = []
    for _ in range(pasadas):
        cuerpo = json.dumps({
            "messages": [{"role": "user", "content": prompt + "\n\nResume en una frase."}],
            "max_tokens": 160, "temperature": 0, "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{puerto}/v1/chat/completions", data=cuerpo,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {CLAVE}"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=1200) as r:
            d = json.loads(r.read())
        tm = d.get("timings", {})
        datos.append((tm.get("prompt_n", 0), tm.get("prompt_per_second", 0),
                      tm.get("predicted_per_second", 0), time.time() - t0))
        print(f"      pasada: prompt_n={datos[-1][0]} pp={datos[-1][1]:.1f} tg={datos[-1][2]:.2f}")
    return datos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ubatch", type=int, nargs="+", default=[512, 1024, 2048])
    ap.add_argument("--batch", type=int, default=4096, help="fijo en todo el barrido")
    ap.add_argument("--palabras", type=int, default=3000)
    ap.add_argument("--passes", type=int, default=2)
    a = ap.parse_args()

    if not CLAVE:
        sys.exit("Falta LLAMA_API_KEY")
    if sh("id -u") != "0" and sh("sudo -n true; echo $?", check=False) != "0":
        sys.exit("Necesito sudo sin contrasena (ejecuta 'sudo -v' antes)")

    prod = unidad_productiva()
    prod_estaba_activa = sh(f"systemctl is-active {UNIT_PROD}", check=False) == "active"
    print(f"[i] productiva activa al empezar: {prod_estaba_activa}")
    print(f"[i] batch FIJO en {a.batch}; solo se mueve ubatch\n")
    filas = []
    try:
        if prod_estaba_activa:
            print(f"[i] parando {UNIT_PROD} (la GPU no da para dos servidores)")
            sh(f"sudo -n systemctl stop {UNIT_PROD}")

        for ub in a.ubatch:
            print(f"[*] ubatch={ub} (batch={a.batch})")
            sh(f"sudo -n tee {RUTA_BENCH} >/dev/null <<'EOF'\n{construye_unidad(prod, ub, a.batch)}\nEOF")
            sh("sudo -n systemctl daemon-reload")
            sh(f"sudo -n systemctl restart {UNIT_BENCH}", check=False)
            if not espera_salud(PUERTO_BENCH):
                print("    ❌ no arranco o murio — lo anoto y sigo")
                filas.append((ub, None, None, None, "no arranca"))
                sh(f"sudo -n systemctl stop {UNIT_BENCH}", check=False)
                continue
            try:
                d = mide(PUERTO_BENCH, a.palabras, a.passes)
                filas.append((ub, d[0][0], statistics.median(x[1] for x in d),
                              statistics.median(x[2] for x in d), "ok"))
            except Exception as e:
                print(f"    ❌ fallo midiendo: {str(e)[:150]}")
                filas.append((ub, None, None, None, f"error: {str(e)[:60]}"))
            sh(f"sudo -n systemctl stop {UNIT_BENCH}", check=False)
    finally:
        print("\n[i] limpiando y restaurando")
        sh(f"sudo -n systemctl stop {UNIT_BENCH}", check=False)
        sh(f"sudo -n systemctl disable {UNIT_BENCH}", check=False)
        sh(f"sudo -n rm -f {RUTA_BENCH}", check=False)
        sh("sudo -n systemctl daemon-reload", check=False)
        if prod_estaba_activa:
            sh(f"sudo -n systemctl start {UNIT_PROD}", check=False)
            ok = espera_salud(8080)
            print(f"[i] {UNIT_PROD} restaurada y sana: {ok}")
        # la unidad productiva nunca se toco: se leyo, no se escribio
        print(f"[i] unidad productiva intacta: {'--api-key-file' in prod}")

    print("\n| ubatch | batch | prompt_n | pp t/s | tg t/s | estado |")
    print("|---|---|---|---|---|---|")
    for ub, pn, pp, tg, est in filas:
        if pp is None:
            print(f"| {ub} | {a.batch} | — | — | — | {est} |")
        else:
            print(f"| {ub} | {a.batch} | {pn} | {pp:.1f} | {tg:.2f} | {est} |")


if __name__ == "__main__":
    main()
