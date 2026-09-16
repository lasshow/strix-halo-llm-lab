#!/usr/bin/env python3
"""H-040: coexistencia Flash-Next (ctx recortado) + Ornith 35B A3B en el M5.

Fases:
  A  solo Flash-Next  -> tg t/s de referencia
  B  Ornith cargado en reposo -> tg de Flash-Next (coste de tener el otro en RAM)
  B2 Flash-Next en reposo -> tg de Ornith
  C  los dos generando a la vez -> tg de cada uno

Se ejecuta EN EL M5. No toca la unit de systemd: el servicio se para fuera
de este script y se restaura fuera de este script.
"""
import json, os, subprocess, sys, time, threading
import urllib.request

BIN = "/models/llama-current/build/bin/llama-server"
FN_MODEL = "/models/gguf/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf"
FN_MTP = "/models/gguf/qwen38-flash-next/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf"
OR_MODEL = "/models/gguf/ornith15-35b/Ornith-1.5-35B-A3B-Q5_K_M.gguf"
OUT = "/home/lasso/m5-ornith/resultados/coexistencia.jsonl"
LOGDIR = "/home/lasso/m5-ornith/logs"
CTX = 32768

PROMPTS = [
    ("py_primes", "Escribe una funcion Python criba_eratostenes(n) que devuelva la lista de primos menores que n. Solo codigo, sin explicaciones.", 400),
    ("rs_wordcount", "Escribe un programa en Rust que lea stdin y escriba las 10 palabras mas frecuentes con su conteo. Solo codigo.", 500),
    ("raz_tasas", "Un modelo genera a 25 tokens/s y otro a 64 tokens/s. Para una respuesta de 1500 tokens, cuanto tarda cada uno y cuanto se ahorra. Razona paso a paso.", 500),
]


def lanzar(nombre, args, log):
    f = open(log, "wb")
    p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"[{nombre}] pid {p.pid}", flush=True)
    return p


def espera_listo(puerto, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(5)
    return False


def genera(puerto, pid_prompt, max_tok):
    pid, prompt = pid_prompt[0], pid_prompt[1]
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tok, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{puerto}/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read())
    dt = time.time() - t0
    u = d.get("usage", {})
    tok = u.get("completion_tokens", 0)
    return {"id": pid, "segundos": round(dt, 2), "tok_salida": tok,
            "tps": round(tok / dt, 2) if dt else 0,
            "finish_reason": d["choices"][0].get("finish_reason"),
            "vacio": not (d["choices"][0]["message"].get("content") or "").strip()}


def mem():
    g = {}
    for ln in open("/proc/meminfo"):
        k, v = ln.split(":")
        g[k] = int(v.split()[0]) // 1024
    gtt = vram = 0
    import glob
    for f in glob.glob("/sys/class/drm/card*/device/mem_info_gtt_used"):
        gtt = max(gtt, int(open(f).read()) // 1024 // 1024)
    for f in glob.glob("/sys/class/drm/card*/device/mem_info_vram_used"):
        vram = max(vram, int(open(f).read()) // 1024 // 1024)
    return {"mem_usada_mb": g["MemTotal"] - g["MemAvailable"], "gtt_mb": gtt, "vram_mb": vram}


def anota(fase, motor, r, extra=None):
    reg = {"fase": fase, "motor": motor, "ts": time.strftime("%F %T")}
    reg.update(r)
    reg.update(mem())
    if extra:
        reg.update(extra)
    with open(OUT, "a") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    print(f"[{fase}][{motor}] {r['id']} {r['segundos']}s {r['tok_salida']}tok "
          f"{r['tps']}t/s fin={r['finish_reason']} vacio={r['vacio']}", flush=True)
    return reg


def serie(fase, motor, puerto):
    for p in PROMPTS:
        anota(fase, motor, genera(puerto, p, p[2]))


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    fn = lanzar("flashnext", [
        BIN, "--model", FN_MODEL,
        "--spec-type", "draft-mtp", "-md", FN_MTP,
        "--spec-draft-n-max", "2", "--spec-draft-p-min", "0",
        "--alias", "qwen3.8-flash-next", "-ngl", "99", "-c", str(CTX),
        "-np", "1", "-fa", "on", "--no-context-shift", "--lazy-mode", "off",
        "--threads", "16", "--host", "127.0.0.1", "--port", "8080",
    ], f"{LOGDIR}/coex-flashnext.log")
    if not espera_listo(8080):
        print("FN no arranca", flush=True); return 1
    print("MEM tras FN:", mem(), flush=True)

    print("== FASE A: solo Flash-Next ==", flush=True)
    serie("A_solo", "flashnext", 8080)

    orn = lanzar("ornith", [
        BIN, "--model", OR_MODEL, "--alias", "ornith-35b-a3b",
        "-ngl", "99", "-c", str(CTX), "-np", "1", "-fa", "on",
        "--no-context-shift", "--threads", "16",
        "--host", "127.0.0.1", "--port", "8081",
    ], f"{LOGDIR}/coex-ornith.log")
    if not espera_listo(8081):
        print("ORNITH no arranca (probable falta de RAM)", flush=True)
        print("MEM:", mem(), flush=True)
        fn.terminate(); orn.terminate(); return 2
    print("MEM con LOS DOS cargados:", mem(), flush=True)

    print("== FASE B: Flash-Next con Ornith cargado en reposo ==", flush=True)
    serie("B_coresidente", "flashnext", 8080)
    print("== FASE B2: Ornith con Flash-Next cargado en reposo ==", flush=True)
    serie("B_coresidente", "ornith", 8081)

    print("== FASE C: los dos generando a la vez ==", flush=True)
    for p in PROMPTS:
        res = {}
        def w(motor, puerto):
            res[motor] = genera(puerto, p, p[2])
        h1 = threading.Thread(target=w, args=("flashnext", 8080))
        h2 = threading.Thread(target=w, args=("ornith", 8081))
        h1.start(); h2.start(); h1.join(); h2.join()
        anota("C_simultaneo", "flashnext", res["flashnext"])
        anota("C_simultaneo", "ornith", res["ornith"])

    print("== fin, parando motores ==", flush=True)
    for p in (fn, orn):
        p.terminate()
    time.sleep(10)
    for p in (fn, orn):
        if p.poll() is None:
            p.kill()
    print("LISTO", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
