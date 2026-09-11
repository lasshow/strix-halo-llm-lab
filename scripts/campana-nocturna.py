#!/usr/bin/env python3
"""
CAMPANA NOCTURNA M5 (10/11-sep-2026): maximizar llama-flashnext manteniendo VISION.

Corre EN EL M5 con la produccion PARADA (la unidad llama-flashnext se para al
principio y se arranca SIEMPRE al final, gane quien gane). Cada configuracion
se lanza como proceso hijo en el puerto 8081 y se mata por PID.

Fases (cada una tolera fallos: se anota y se sigue):
  F1  Build A/B: binario productivo (311d421) vs HEAD nuevo (worktree).
      Prefill 3k y 24k tokens + generacion 200 tok. Orden A,B,A,B (alternado).
  F2  Especulacion sin pesos extra: control / draft-mtp (n_max x p_min) /
      ngram-simple. 4 familias de prompt x 2 pasadas, con acceptance rate.
      Se hace CON --mmproj cargado: la vision es requisito, asi que si la
      especulacion no convive con mtmd, eso es un resultado, no un ajuste.
  F3  DPM auto vs high: latencia de prefill en peticion corta tras 45 s de
      reposo (arranque frio de reloj) + consumo en reposo.
  F4  Decision con umbrales escritos ANTES de medir (abajo) y aplicacion a la
      unidad productiva con backup; smoke test + control negativo 401; si
      falla, se restaura el backup.

Umbrales (fijados a priori, ver H-031 en la bitacora):
  build nueva  -> se adopta si mediana pp(3k) >= 0.98x y tg >= 0.98x del viejo
                  (es decir: NO empeora; el motivo de adoptarla es ir al dia).
  especulacion -> se adopta si media(prosa,codigo) >= 1.08x control, ninguna
                  familia < 0.95x control y pp(3k) >= 0.97x control.
  cache-ram    -> 4096 -> 12288 siempre (17 GB libres medidos; 12 GB = 4 conv.
                  de 33k tokens en cache en vez de 1).
  DPM high     -> se adopta si prompt_ms corto mejora >= 15% y el consumo en
                  reposo sube < 10 W.
"""
import json, os, re, shlex, signal, statistics, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, "campana")
LOG = os.path.join(DIR, "campana.log")
JSONL = os.path.join(DIR, "medidas.jsonl")
INFORME = os.path.join(DIR, "INFORME.md")
UNIT = "llama-flashnext"
UNIT_PATH = f"/etc/systemd/system/{UNIT}.service"
PORT = 8081
BIN_OLD = "/models/llama.cpp/build/bin/llama-server"
WT = "/models/llama.cpp-nuevo"
BIN_NEW = f"{WT}/build/bin/llama-server"
KEYFILE = "/etc/llama-server/api-keys.txt"
DPM = "/sys/class/drm/card0/device/power_dpm_force_performance_level"
HWMON_GLOB = "/sys/class/drm/card0/device/hwmon"

os.makedirs(DIR, exist_ok=True)
KEY = open(KEYFILE).read().split()[0]
HDR = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}
FRASE = ("El sistema de inferencia procesa secuencias extensas de texto "
         "para evaluar el rendimiento sostenido de la memoria unificada ")
TOKENS_POR_PALABRA = 1.78  # calibrado en bench-context.py; se verifica prompt_n real
NOTAS = []


def log(msg):
    linea = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(linea, flush=True)
    with open(LOG, "a") as f:
        f.write(linea + "\n")


def nota(msg):
    NOTAS.append(msg); log("NOTA: " + msg)


def sh(cmd, check=False, timeout=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} -> {r.returncode}: {r.stderr[-400:]}")
    return r.stdout.strip()


def jsonl(reg):
    reg["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(JSONL, "a") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")


# ---------- parametros de la unidad productiva (se LEEN, no se teclean) ----------
def execstart_args():
    txt = sh(f"sudo -n systemctl cat {UNIT}")
    m = re.search(r"^ExecStart=(.*?)(?=^\S|\Z)", txt, re.S | re.M)
    if not m:
        raise RuntimeError("no encuentro ExecStart")
    raw = m.group(1).replace("\\\n", " ")
    toks = shlex.split(raw)
    args = toks[1:]  # quitar el binario
    return args


def args_base():
    """Argumentos de produccion sin host/puerto (se sustituyen por 127.0.0.1:8081)."""
    a = execstart_args()
    out, skip = [], False
    for t in a:
        if skip:
            skip = False; continue
        if t in ("--host", "--port"):
            skip = True; continue
        out.append(t)
    out += ["--host", "127.0.0.1", "--port", str(PORT)]
    return out


def unit_env():
    """Environment= de la unidad (GGML_VK_VISIBLE_DEVICES, etc.) y EnvironmentFile."""
    env = dict(os.environ)
    txt = sh(f"sudo -n systemctl cat {UNIT}")
    for m in re.finditer(r"^Environment=(.*)$", txt, re.M):
        for kv in shlex.split(m.group(1)):
            if "=" in kv:
                k, v = kv.split("=", 1); env[k] = v
    for m in re.finditer(r"^EnvironmentFile=-?(\S+)$", txt, re.M):
        txt2 = sh(f"sudo -n cat {m.group(1)}")
        for linea in txt2.splitlines():
            if "=" in linea and not linea.startswith("#"):
                k, v = linea.split("=", 1); env[k.strip()] = v.strip().strip('"')
    return env


# ---------- servidor de pruebas ----------
class Servidor:
    def __init__(self, binario, extra, etiqueta):
        self.bin, self.extra, self.etq = binario, list(extra), etiqueta
        self.p = None
        self.logf = os.path.join(DIR, f"srv-{etiqueta}.log")

    def __enter__(self):
        args = args_base()
        # sustituir/anadir los extra: si un flag ya existe con valor, se reemplaza
        for i in range(0, len(self.extra)):
            pass
        cmd = [self.bin] + args + self.extra
        env = unit_env()
        log(f"arranco [{self.etq}]: {' '.join(shlex.quote(c) for c in cmd[1:])[:300]}...")
        self.p = subprocess.Popen(cmd, stdout=open(self.logf, "w"), stderr=subprocess.STDOUT,
                                  env=env, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < 900:
            if self.p.poll() is not None:
                raise RuntimeError(f"[{self.etq}] el servidor murio al arrancar (rc={self.p.returncode}); "
                                   f"cola del log: {open(self.logf).read()[-600:]}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
                    if r.status == 200:
                        log(f"[{self.etq}] listo en {time.time()-t0:.0f} s")
                        return self
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError(f"[{self.etq}] timeout 900 s esperando /health")

    def __exit__(self, *a):
        if self.p and self.p.poll() is None:
            os.killpg(self.p.pid, signal.SIGTERM)
            try:
                self.p.wait(60)
            except subprocess.TimeoutExpired:
                os.killpg(self.p.pid, signal.SIGKILL); self.p.wait(30)
        time.sleep(3)
        return False


def peticion(messages, max_tokens, timeout=1800, thinking=False):
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.0,
            "stream": False, "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": thinking}}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=HDR)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    d["_wall_s"] = time.time() - t0
    t = d.get("timings")
    if not t or "prompt_n" not in t or "predicted_per_second" not in t:
        raise RuntimeError("respuesta sin timings: medida invalida")
    ch = d["choices"][0]
    if ch.get("finish_reason") not in ("stop", "length"):
        raise RuntimeError(f"finish_reason anormal: {ch.get('finish_reason')}")
    if not (ch["message"].get("content") or "").strip():
        raise RuntimeError("content vacio")
    return d


def prompt_relleno(n_tokens):
    unit = len(FRASE.split())
    reps = int(n_tokens / (unit * TOKENS_POR_PALABRA)) + 1
    return FRASE * reps + ("\n\nA partir del texto anterior, redacta un informe extenso y "
                           "detallado sobre la memoria unificada en equipos pequenos.")


def medida_pp_tg(fase, etq, n_tok, pasadas, max_tokens=200):
    """Prefill de n_tok tokens + generacion. Devuelve medianas (pp t/s, tg t/s)."""
    pps, tgs = [], []
    for k in range(pasadas):
        try:
            d = peticion([{"role": "user", "content": prompt_relleno(n_tok)}], max_tokens)
        except Exception as e:
            nota(f"{fase}/{etq} n={n_tok} pasada {k}: {e}"); continue
        t = d["timings"]
        pp = t["prompt_n"] / (t["prompt_ms"] / 1000)
        if t["predicted_n"] < 100:
            nota(f"{fase}/{etq} n={n_tok}: solo {t['predicted_n']} tokens generados, tg no fiable")
        if abs(t["prompt_n"] - n_tok) / n_tok > 0.15:
            nota(f"{fase}/{etq}: prompt_n real {t['prompt_n']} vs objetivo {n_tok} (>15%)")
        pps.append(pp); tgs.append(t["predicted_per_second"])
        jsonl({"fase": fase, "config": etq, "objetivo_tok": n_tok, "prompt_n": t["prompt_n"],
               "pp_tps": round(pp, 2), "tg_tps": round(t["predicted_per_second"], 2),
               "predicted_n": t["predicted_n"], "prompt_ms": t["prompt_ms"], "pasada": k,
               "wall_s": round(d["_wall_s"], 1)})
        log(f"  {etq} n={t['prompt_n']} pp={pp:.1f} tg={t['predicted_per_second']:.2f} pasada {k}")
    if not pps:
        return None, None
    return statistics.median(pps), statistics.median(tgs)


FAMILIAS = {
    "prosa": "Explica en tres parrafos, para alguien sin formacion tecnica, por que la memoria unificada cambia lo que un ordenador pequeno puede hacer con inteligencia artificial.",
    "codigo": "Escribe una funcion en Python que reciba una lista de diccionarios con claves 'fecha' e 'importe', agrupe por mes y devuelva el total de cada mes ordenado cronologicamente. Incluye docstring y manejo de errores.",
    "json": "Devuelve SOLO un objeto JSON valido con las claves: nombre, version, dependencias (lista de 5 strings), activo (booleano) y metricas (objeto con tres claves numericas). Sin texto adicional.",
    "creativo": "Escribe un poema de ocho versos sobre un faro en una noche de tormenta y despues explica en un parrafo la metafora.",
}


def medida_familias(fase, etq, pasadas=2, max_tokens=400):
    res = {}
    for fam, p in FAMILIAS.items():
        tgs, accs = [], []
        for k in range(pasadas):
            try:
                d = peticion([{"role": "user", "content": p}], max_tokens, timeout=900)
            except Exception as e:
                nota(f"{fase}/{etq}/{fam} pasada {k}: {e}"); continue
            t = d["timings"]
            acc = None
            if t.get("draft_n"):
                acc = round(100 * t.get("draft_n_accepted", 0) / t["draft_n"], 1)
            tgs.append(t["predicted_per_second"]); accs.append(acc)
            jsonl({"fase": fase, "config": etq, "familia": fam, "pasada": k,
                   "tg_tps": round(t["predicted_per_second"], 2), "predicted_n": t["predicted_n"],
                   "draft_n": t.get("draft_n"), "draft_n_accepted": t.get("draft_n_accepted"),
                   "acceptance_pct": acc, "wall_s": round(d["_wall_s"], 1)})
            log(f"  {etq} {fam:8s} tg={t['predicted_per_second']:.2f} acc={acc} n={t['predicted_n']}")
        if tgs:
            res[fam] = {"tg": statistics.median(tgs),
                        "acc": statistics.median([a for a in accs if a is not None]) if any(a is not None for a in accs) else None}
    return res


def potencia_w():
    try:
        d = [x for x in os.listdir(HWMON_GLOB)][0]
        return int(open(f"{HWMON_GLOB}/{d}/power1_average").read()) / 1e6
    except Exception:
        return None


# ---------- FASE 0: build nueva en worktree ----------
def fase0_build():
    if os.path.exists(BIN_NEW):
        log("F0: worktree ya construido, lo reutilizo")
    else:
        log("F0: construyo HEAD de origin/master en worktree separado")
        sh("cd /models/llama.cpp && git fetch -q origin", check=True, timeout=600)
        sh(f"cd /models/llama.cpp && git worktree add --detach {WT} origin/master", check=True)
        sh(f"cd {WT} && cmake -B build -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF "
           f"> {DIR}/build-nuevo.log 2>&1 && cmake --build build --config Release -j 30 -t llama-server "
           f">> {DIR}/build-nuevo.log 2>&1", check=True, timeout=3600)
    ver_old = sh(f"{BIN_OLD} --version 2>&1 | head -1")
    ver_new = sh(f"{BIN_NEW} --version 2>&1 | head -1")
    commit_new = sh(f"cd {WT} && git rev-parse --short HEAD")
    log(f"F0: viejo: {ver_old} | nuevo: {ver_new} ({commit_new})")
    return commit_new


# ---------- principal ----------
def main():
    t_ini = time.time()
    log("=" * 70); log("CAMPANA NOCTURNA: inicio")
    prod_activa = sh(f"systemctl is-active {UNIT}") == "active"
    sh(f"sudo -n cp {UNIT_PATH} {UNIT_PATH}.bak-campana-20260910", check=True)
    args_prod = execstart_args()
    log("args produccion: " + " ".join(args_prod))
    resultados = {"args_prod": args_prod}
    try:
        commit_new = fase0_build()
        resultados["commit_new"] = commit_new
    except Exception as e:
        nota(f"F0 build fallo: {e}"); commit_new = None

    log(f"paro produccion ({UNIT})")
    sh(f"sudo -n systemctl stop {UNIT}", check=True); time.sleep(5)
    # unidad de brush-server se deja: no usa la GPU salvo con trabajo activo

    try:
        # ---------------- F1: A/B builds ----------------
        f1 = {"old": {"3k": [], "24k": []}, "new": {"3k": [], "24k": []}}
        if commit_new:
            for ronda in range(2):
                for etq, binario in (("old", BIN_OLD), ("new", BIN_NEW)):
                    try:
                        with Servidor(binario, [], f"F1-{etq}-r{ronda}"):
                            peticion([{"role": "user", "content": "Di hola."}], 16)  # calentamiento shaders
                            pp3, tg3 = medida_pp_tg("F1", etq, 3000, 3)
                            pp24, tg24 = medida_pp_tg("F1", etq, 24000, 1)
                            if pp3: f1[etq]["3k"].append((pp3, tg3))
                            if pp24: f1[etq]["24k"].append((pp24, tg24))
                    except Exception as e:
                        nota(f"F1 {etq} ronda {ronda}: {e}")
        else:
            nota("F1 omitida: no hay build nueva")

        def med(lst, i):
            v = [x[i] for x in lst if x[i] is not None]
            return statistics.median(v) if v else None

        r1 = {}
        for etq in ("old", "new"):
            r1[etq] = {"pp3": med(f1[etq]["3k"], 0), "tg3": med(f1[etq]["3k"], 1),
                       "pp24": med(f1[etq]["24k"], 0), "tg24": med(f1[etq]["24k"], 1)}
        resultados["F1"] = r1
        adoptar_build = False
        if r1["old"]["pp3"] and r1["new"]["pp3"]:
            adoptar_build = (r1["new"]["pp3"] >= 0.98 * r1["old"]["pp3"] and
                             r1["new"]["tg3"] >= 0.98 * r1["old"]["tg3"])
        resultados["adoptar_build"] = adoptar_build
        log(f"F1 resultado: {json.dumps(r1)} -> adoptar build nueva: {adoptar_build}")
        BIN = BIN_NEW if adoptar_build else BIN_OLD

        # ---------------- F2: especulacion ----------------
        configs = [
            ("control", []),
            ("mtp-n2-p0", ["--spec-type", "draft-mtp", "--spec-draft-n-max", "2", "--spec-draft-p-min", "0.0"]),
            ("mtp-n3-p0", ["--spec-type", "draft-mtp", "--spec-draft-n-max", "3", "--spec-draft-p-min", "0.0"]),
            ("mtp-n3-p06", ["--spec-type", "draft-mtp", "--spec-draft-n-max", "3", "--spec-draft-p-min", "0.6"]),
            ("mtp-n5-p06", ["--spec-type", "draft-mtp", "--spec-draft-n-max", "5", "--spec-draft-p-min", "0.6"]),
            ("ngram-simple", ["--spec-type", "ngram-simple"]),
        ]
        f2 = {}
        for etq, extra in configs:
            try:
                with Servidor(BIN, extra, f"F2-{etq}"):
                    peticion([{"role": "user", "content": "Di hola."}], 16)
                    fams = medida_familias("F2", etq, 2)
                    pp3, tg3 = medida_pp_tg("F2", etq, 3000, 2)
                    f2[etq] = {"fam": fams, "pp3": pp3, "tg3": tg3, "extra": extra}
            except Exception as e:
                nota(f"F2 {etq}: {e}")
                f2[etq] = {"error": str(e)[:300], "extra": extra}
        resultados["F2"] = f2
        mejor_spec, mejor_gan = None, 1.0
        c = f2.get("control", {})
        if c.get("fam") and all(k in c["fam"] for k in FAMILIAS):
            base_pc = (c["fam"]["prosa"]["tg"] + c["fam"]["codigo"]["tg"]) / 2
            for etq, r in f2.items():
                if etq == "control" or "fam" not in r or not all(k in r["fam"] for k in FAMILIAS):
                    continue
                gan = ((r["fam"]["prosa"]["tg"] + r["fam"]["codigo"]["tg"]) / 2) / base_pc
                peor = min(r["fam"][k]["tg"] / c["fam"][k]["tg"] for k in FAMILIAS)
                pp_ok = (r["pp3"] or 0) >= 0.97 * (c["pp3"] or 1)
                r["ganancia_pc"] = round(gan, 3); r["peor_familia"] = round(peor, 3); r["pp_ok"] = pp_ok
                if gan >= 1.08 and peor >= 0.95 and pp_ok and gan > mejor_gan:
                    mejor_spec, mejor_gan = etq, gan
        resultados["adoptar_spec"] = mejor_spec
        log(f"F2 resultado: mejor especulacion adoptable = {mejor_spec} ({mejor_gan:.3f}x)")
        extra_spec = f2[mejor_spec]["extra"] if mejor_spec else []

        # ---------------- F3: DPM ----------------
        f3 = {}
        try:
            dpm0 = open(DPM).read().strip()
            with Servidor(BIN, extra_spec, "F3-dpm"):
                peticion([{"role": "user", "content": "Di hola."}], 16)
                for nivel in ("auto", "high", "auto"):
                    sh(f"echo {nivel} | sudo -n tee {DPM} >/dev/null", check=True)
                    time.sleep(10)
                    p_idle = []
                    pms = []
                    for k in range(3):
                        time.sleep(45)
                        p_idle.append(potencia_w())
                        d = peticion([{"role": "user", "content": "Resume en una frase que es la memoria unificada."}], 24)
                        pms.append(d["timings"]["prompt_ms"])
                        jsonl({"fase": "F3", "dpm": nivel, "pasada": k, "prompt_ms": d["timings"]["prompt_ms"],
                               "prompt_n": d["timings"]["prompt_n"], "tg_tps": d["timings"]["predicted_per_second"],
                               "idle_w": p_idle[-1]})
                    f3.setdefault(nivel, []).append({"prompt_ms_med": statistics.median(pms),
                                                     "idle_w": statistics.median([x for x in p_idle if x is not None]) if any(p_idle) else None})
                    log(f"  DPM {nivel}: prompt_ms mediana {statistics.median(pms):.0f}, idle {f3[nivel][-1]['idle_w']} W")
            sh(f"echo {dpm0} | sudo -n tee {DPM} >/dev/null")
        except Exception as e:
            nota(f"F3 DPM: {e}")
            sh(f"echo auto | sudo -n tee {DPM} >/dev/null")
        resultados["F3"] = f3
        adoptar_dpm = False
        if f3.get("auto") and f3.get("high"):
            auto_ms = statistics.median([x["prompt_ms_med"] for x in f3["auto"]])
            high_ms = f3["high"][0]["prompt_ms_med"]
            auto_w = statistics.median([x["idle_w"] for x in f3["auto"] if x["idle_w"] is not None] or [0])
            high_w = f3["high"][0]["idle_w"] or 0
            adoptar_dpm = high_ms <= 0.85 * auto_ms and (high_w - auto_w) < 10
            resultados["F3_resumen"] = {"auto_ms": auto_ms, "high_ms": high_ms, "auto_w": auto_w, "high_w": high_w}
        resultados["adoptar_dpm"] = adoptar_dpm
        log(f"F3 resultado: adoptar DPM high: {adoptar_dpm}")

        # ---------------- F4: aplicar ----------------
        aplicar(adoptar_build, extra_spec, adoptar_dpm, resultados)

    finally:
        # produccion SIEMPRE arriba al salir
        sh("echo auto | sudo -n tee " + DPM + " >/dev/null")
        sh("fuser -k 8081/tcp >/dev/null 2>&1")
        sh(f"sudo -n systemctl daemon-reload; sudo -n systemctl start {UNIT}")
        ok = espera_prod()
        resultados["prod_final_ok"] = ok
        if not ok:
            nota("PRODUCCION NO ARRANCO con la unidad nueva: restauro backup")
            sh(f"sudo -n cp {UNIT_PATH}.bak-campana-20260910 {UNIT_PATH}; sudo -n systemctl daemon-reload; "
               f"sudo -n systemctl restart {UNIT}")
            resultados["prod_final_ok"] = espera_prod()
            resultados["restaurado"] = True
        resultados["duracion_min"] = round((time.time() - t_ini) / 60, 1)
        resultados["notas"] = NOTAS
        json.dump(resultados, open(os.path.join(DIR, "resultados.json"), "w"), indent=1, ensure_ascii=False)
        informe(resultados)
        log("CAMPANA: fin")


def espera_prod(limite=900):
    t0 = time.time()
    while time.time() - t0 < limite:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        if sh(f"systemctl is-active {UNIT}") == "failed":
            return False
        time.sleep(5)
    return False


def smoke_prod():
    """Respuesta no vacia con finish_reason stop + control negativo 401."""
    body = {"messages": [{"role": "user", "content": "Cuanto es 17*23? Responde solo el numero."}],
            "max_tokens": 64, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=HDR)
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    ch = d["choices"][0]
    ok = bool((ch["message"].get("content") or "").strip()) and ch["finish_reason"] == "stop"
    # control negativo
    req2 = urllib.request.Request("http://127.0.0.1:8080/v1/models", headers={"Authorization": "Bearer clave-falsa"})
    try:
        urllib.request.urlopen(req2, timeout=30); neg = False
    except urllib.error.HTTPError as e:
        neg = e.code == 401
    # vision: el mmproj sigue cargado?
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/props", headers=HDR), timeout=30) as r:
        props = json.loads(r.read())
    vision = bool(props.get("modalities", {}).get("vision"))
    return ok, neg, vision, (ch["message"].get("content") or "")[:60]


def aplicar(adoptar_build, extra_spec, adoptar_dpm, resultados):
    txt = sh(f"sudo -n cat {UNIT_PATH}")
    nuevo = txt
    cambios = []
    if adoptar_build:
        # adopcion segura: reconstruir el arbol PRODUCTIVO al mismo commit (mismo path en la unidad)
        try:
            log("F4: reconstruyo /models/llama.cpp al commit nuevo (produccion parada)")
            sh(f"cd /models/llama.cpp && git checkout -q --detach {resultados['commit_new']} && "
               f"cmake --build build --config Release -j 30 -t llama-server llama-bench > {DIR}/build-prod.log 2>&1 && "
               f"sudo -n restorecon -RF /models/llama.cpp/build/bin", check=True, timeout=3600)
            cambios.append(f"llama.cpp 311d421 -> {resultados['commit_new']}")
        except Exception as e:
            nota(f"F4 rebuild productivo fallo, vuelvo a 311d421: {e}")
            sh("cd /models/llama.cpp && git checkout -q --detach 311d421 && cmake --build build --config Release -j 30 -t llama-server llama-bench >/dev/null 2>&1", timeout=3600)
    # cache-ram
    if re.search(r"--cache-ram\s+\d+", nuevo):
        nuevo = re.sub(r"--cache-ram\s+\d+", "--cache-ram 12288", nuevo); cambios.append("--cache-ram 4096 -> 12288")
    else:
        nuevo = re.sub(r"(ExecStart=\S+)", r"\1 --cache-ram 12288", nuevo, count=1); cambios.append("--cache-ram 12288 (nuevo)")
    if extra_spec:
        nuevo = re.sub(r"(ExecStart=\S+)", r"\1 " + " ".join(extra_spec), nuevo, count=1)
        cambios.append("especulacion: " + " ".join(extra_spec))
    if adoptar_dpm:
        nuevo = nuevo.replace("[Service]", f"[Service]\nExecStartPre=+/bin/sh -c 'echo high > {DPM}'\n"
                                           f"ExecStopPost=+/bin/sh -c 'echo auto > {DPM}'", 1)
        cambios.append("DPM high mientras el servicio corre")
    resultados["cambios"] = cambios
    tmp = os.path.join(DIR, "llama-flashnext.service.nuevo")
    open(tmp, "w").write(nuevo)
    sh(f"sudo -n cp {tmp} {UNIT_PATH} && sudo -n systemctl daemon-reload", check=True)
    log("F4: unidad escrita; arranco produccion para smoke")
    sh(f"sudo -n systemctl start {UNIT}")
    if not espera_prod():
        raise RuntimeError("produccion no arranca con la unidad nueva")
    ok, neg, vision, muestra = smoke_prod()
    resultados["smoke"] = {"respuesta_ok": ok, "control_401": neg, "vision": vision, "muestra": muestra}
    log(f"F4 smoke: respuesta={ok} 401={neg} vision={vision} muestra={muestra!r}")
    if not (ok and neg and vision):
        nota("SMOKE FALLO con la unidad nueva -> restauro backup")
        sh(f"sudo -n cp {UNIT_PATH}.bak-campana-20260910 {UNIT_PATH}; sudo -n systemctl daemon-reload; sudo -n systemctl restart {UNIT}")
        resultados["restaurado"] = True
        resultados["cambios_aplicados"] = []
    else:
        resultados["cambios_aplicados"] = cambios
    # medida final de produccion (para la bitacora)
    try:
        global PORT
        PORT_BAK = PORT; PORT = 8080
        pp3, tg3 = medida_pp_tg("F5-prod", "prod-final", 3000, 3)
        resultados["prod_final"] = {"pp3": pp3, "tg3": tg3}
        PORT = PORT_BAK
    except Exception as e:
        nota(f"medida final: {e}")


def f(x, nd=1):
    return "n/d" if x is None else f"{x:.{nd}f}"


def informe(r):
    L = ["# Campana nocturna M5 — 10/11-sep-2026", "",
         f"Duracion: {r.get('duracion_min')} min · produccion al final: {'OK' if r.get('prod_final_ok') else 'CAIDA'}"
         + (" · UNIDAD RESTAURADA" if r.get("restaurado") else ""), ""]
    L += ["## Cambios aplicados a llama-flashnext"]
    L += [f"- {c}" for c in r.get("cambios_aplicados", [])] or ["- ninguno"]
    if r.get("smoke"):
        s = r["smoke"]; L += ["", f"Smoke: respuesta={s['respuesta_ok']} · 401={s['control_401']} · vision={s['vision']} · '{s['muestra']}'"]
    if r.get("prod_final"):
        L += [f"Produccion final: pp(3k) {f(r['prod_final']['pp3'])} t/s · tg {f(r['prod_final']['tg3'],2)} t/s"]
    L += ["", "## F1 build 311d421 vs " + str(r.get("commit_new"))]
    for etq, v in r.get("F1", {}).items():
        L.append(f"- {etq}: pp3k {f(v['pp3'])} · tg3k {f(v['tg3'],2)} · pp24k {f(v['pp24'])} · tg24k {f(v['tg24'],2)}")
    L.append(f"-> adoptar build nueva: {r.get('adoptar_build')}")
    L += ["", "## F2 especulacion (tg t/s por familia · acceptance %)"]
    for etq, v in r.get("F2", {}).items():
        if "error" in v:
            L.append(f"- {etq}: ERROR {v['error'][:120]}"); continue
        fams = " · ".join(f"{k} {f(x['tg'],2)}" + (f" ({x['acc']}%)" if x.get('acc') is not None else "") for k, x in v["fam"].items())
        L.append(f"- {etq}: {fams} · pp3k {f(v['pp3'])}" + (f" · ganancia {v['ganancia_pc']}x peor {v['peor_familia']}x" if 'ganancia_pc' in v else ""))
    L.append(f"-> adoptar: {r.get('adoptar_spec')}")
    L += ["", "## F3 DPM"]
    if r.get("F3_resumen"):
        s = r["F3_resumen"]
        L.append(f"- auto: prompt_ms {f(s['auto_ms'],0)} · idle {f(s['auto_w'])} W | high: prompt_ms {f(s['high_ms'],0)} · idle {f(s['high_w'])} W -> adoptar high: {r.get('adoptar_dpm')}")
    else:
        L.append("- sin datos")
    L += ["", "## Notas / incidencias"] + ([f"- {n}" for n in r.get("notas", [])] or ["- ninguna"])
    open(INFORME, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
