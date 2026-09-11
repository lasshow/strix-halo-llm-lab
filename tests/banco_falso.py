#!/usr/bin/env python3
"""`llama-server` de mentira: el "binario" que levantan las pruebas de H-032/H-033.

Se copia dentro del arbol de builds (`<dir>/<sha>/build/bin/llama-server`) para
que `ctx.binario("baseline"|"candidato")` lo encuentre donde lo encontraria el
binario real, y DEDUCE SU BRAZO de su propia ruta: asi un A/B de dos builds se
puede simular sin compilar nada y sin que el banco sepa que esta hablando con
un impostor.

Lee sus argumentos como los leeria llama-server (`--port`, `--api-key-file`,
`--cache-ram`, `-np`, `-kvu`, `--alias`) y su comportamiento de un JSON apuntado
por `BANCO_FALSO_CONF`:

    modelo_servido        id que devuelve /v1/models (por defecto, el --alias)
    soporta_env_api_key   si su --help menciona LLAMA_ARG_API_KEY
    morir                 codigo de salida inmediato (arranque que falla)
    contamina             responde con el nonce de OTRA conversacion
    pp / tg               {"por_defecto": x, "<sha>": y} tokens/s por brazo
    pp_factor_desde_2     multiplica pp de la 2ª peticion en adelante
    prompt_ms_frio        TTFT sin cache
    prompt_ms_cache       {"por_defecto": x, "<cache-ram>": y} TTFT con cache
    greedy_prefijo        {"por_defecto": "G", "<sha>": "H"} salida greedy

H-034 (MTP sidecar y vision). El modo MTP se ACTIVA SOLO cuando los args del
servidor traen `--spec-type draft-mtp` Y `-md`/`--model-draft`:

    acceptance            acceptance del borrador (0,66): draft_n_accepted =
                          round(draft_n * acceptance) en timings, y el log del
                          proceso imprime "draft acceptance = 0.6600"
    mtp_factor            multiplica `tg` del brazo MTP (1,0 = igual)
    mtp_tg_por_familia   {"<familia>": factor} sobre el de MTP: una familia
                         concreta (prosa/codigo/json/reescritura/creativo)
                         puede hundirse por debajo del suelo aunque el resto
                         mejore
    mtp_greedy_distinto   el content del brazo MTP difiere en un caracter
    mtp_sin_efecto        args MTP pero SIN draft_n ni linea de log: la fase
                          tiene que marcar "especulacion NO activa" como error
    caida_en_ctx          os._exit(1) cuando prompt_n >= este umbral (simula el
                          DeviceLost de #27306)
    vision_mtp_rompe      con imagen y brazo MTP responde HTTP 500 en vez del
                          color; sin imagen responde el color ("Rojo")

Cada peticion recibida y el arranque se anotan en el JSONL de
`BANCO_FALSO_REGISTRO`, para que una prueba pueda comprobar QUE se pidio
(`cache_prompt`, `enable_thinking`, `seed`, `max_tokens`) y no solo que la fase
devolvio un numero bonito.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AYUDA = """usage: llama-server [options]

  --port PORT               puerto de escucha
  --api-key-file FNAME      fichero con la clave de API
  --cache-ram N             cache de prompt en RAM (MiB)
{extra}"""
EXTRA_ENV = ("\nenvironment variables:\n"
             "  LLAMA_ARG_API_KEY         clave de API (equivale a --api-key)\n")

RE_CODIGO = re.compile(r"CÓDIGO-([A-Z]):\s*([0-9a-fA-F]{12})")
RE_PN = re.compile(r"\[\[PN=(\d+)\]\]")

CONF: dict = {}
ARGS: dict = {}
CLAVE = ""
BRAZO = "por_defecto"
CERROJO = threading.Lock()
ESTADO = {"peticiones": 0, "vistos": {}, "prefijos": set()}


# ------------------------------------------------------------------ arranque
def lee_conf() -> dict:
    ruta = os.environ.get("BANCO_FALSO_CONF")
    if not ruta or not os.path.exists(ruta):
        return {}
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def por_brazo(nombre, defecto=None):
    v = CONF.get(nombre)
    if not isinstance(v, dict):
        return v if v is not None else defecto
    return v.get(BRAZO, v.get("por_defecto", defecto))


def parsea(argv: list[str]) -> dict:
    d = {"kvu": False}
    i = 0
    while i < len(argv):
        t = argv[i]
        base, _, pegado = t.partition("=")
        valor = pegado if pegado else (argv[i + 1] if i + 1 < len(argv) else None)
        consume = 1 if pegado else 2
        if base in ("--port",):
            d["port"] = int(valor); i += consume; continue
        if base in ("--host",):
            d["host"] = valor; i += consume; continue
        if base in ("--alias",):
            d["alias"] = valor; i += consume; continue
        if base in ("--api-key-file",):
            d["api_key_file"] = valor; i += consume; continue
        if base in ("--api-key",):
            d["api_key"] = valor; i += consume; continue
        if base in ("--cache-ram",):
            d["cache_ram"] = int(valor); i += consume; continue
        if base in ("-np", "--parallel"):
            d["parallel"] = int(valor); i += consume; continue
        if base in ("-kvu", "--kv-unified"):
            d["kvu"] = True; i += 1; continue
        if base in ("--spec-type",):
            d["spec_type"] = valor; i += consume; continue
        if base in ("-md", "--model-draft"):
            d["draft_model"] = valor; i += consume; continue
        if base in ("--spec-draft-n-max",):
            d["spec_draft_n_max"] = int(valor); i += consume; continue
        if base in ("--spec-draft-p-min",):
            d["spec_draft_p_min"] = float(valor); i += consume; continue
        i += 1
    return d


def mtp_activo() -> bool:
    """¿Este arranque lleva la cabeza de borrador? Solo con los dos flags."""
    return ARGS.get("spec_type") == "draft-mtp" and bool(ARGS.get("draft_model"))


# H-034: para poder simular "una familia se hunde", el fake reconoce a que
# familia pertenece el prompt por una marca distintiva de cada texto.
_FAMILIAS = (
    ("prosa", "Resume en tres frases"),
    ("codigo", "media_por_zona"),
    ("json", "Transforma esta lista en JSON"),
    ("reescritura", "Devuelve el bloque siguiente"),
    ("creativo", "horno industrial"),
)


def familia_de(texto: str) -> str | None:
    for nombre, marca in _FAMILIAS:
        if marca in texto:
            return nombre
    return None


# H-035: texto largo determinista (mismo en control y MTP), tokenizado por
# palabras, y una divergencia colocable en una posicion concreta.
def _texto_largo(fam: str) -> str:
    return " ".join(f"{fam}{i}" for i in range(40))


def _tokens_logprobs(contenido: str, divergencia: dict | None) -> list[dict]:
    """Un token por palabra (con su espacio). El control da top1 -0,1 y un
    segundo -0,4 ("alt"). Con `divergencia` = {"posicion": i, "clase": ...}
    el brazo MTP cambia el token i:
      clase "empate":        el token pasa a ser el segundo del top del control
                             (que esta a 0,3 nats: dentro de un margen de 0,5)
      clase "no_verificado": el token es uno que NO esta en el top del control
      clase "lejano":        el token es el ultimo del top, a 3 nats
    """
    palabras = contenido.split(" ")
    salida = []
    pos = int(divergencia.get("posicion", -1)) if divergencia else -1
    clase = (divergencia or {}).get("clase")
    for i, p in enumerate(palabras):
        tok = (" " if i else "") + p
        top = {tok: -0.1, tok + "_alt": -0.4, tok + "_lejos": -3.1}
        elegido, lp = tok, -0.1
        if i == pos:
            if clase == "empate":
                elegido, lp = tok + "_alt", -0.4
            elif clase == "lejano":
                elegido, lp = tok + "_lejos", -3.1
            elif clase == "no_verificado":
                elegido, lp = tok + "_fuera", -0.2
        salida.append({"token": elegido, "logprob": lp,
                       "top_logprobs": [{"token": k, "logprob": v} for k, v in top.items()]})
    return salida


def anota(reg: dict) -> None:
    ruta = os.environ.get("BANCO_FALSO_REGISTRO")
    if not ruta:
        return
    with CERROJO:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(reg, brazo=BRAZO), ensure_ascii=False) + "\n")


# ----------------------------------------------------------------- servidor
class Manejador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _envia(self, codigo, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        return (not CLAVE) or self.headers.get("Authorization") == f"Bearer {CLAVE}"

    def do_GET(self):
        if self.path == "/health":
            return self._envia(200, {"status": "ok"})
        if not self._auth():
            return self._envia(401, {"error": {"message": "clave invalida"}})
        if self.path == "/v1/models":
            servido = (CONF.get("modelo_servido")
                       or ARGS.get("alias") or "modelo-de-pruebas")
            return self._envia(200, {"data": [{"id": servido}]})
        return self._envia(404, {})

    def do_POST(self):
        if not self._auth():
            return self._envia(401, {"error": {"message": "clave invalida"}})
        n = int(self.headers.get("Content-Length", 0))
        try:
            cuerpo = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._envia(400, {"error": {"message": "cuerpo ilegible"}})
        anota({"evento": "peticion", "ruta": self.path, "cuerpo": cuerpo})
        if not self.path.endswith("/chat/completions"):
            return self._envia(404, {})
        r = responde(cuerpo)
        codigo = r.pop("_http", 200)
        return self._envia(codigo, r)


def _tiene_imagen(mensajes) -> bool:
    for m in mensajes or []:
        c = m.get("content")
        if isinstance(c, list):
            for parte in c:
                if isinstance(parte, dict) and parte.get("type") == "image_url":
                    return True
    return False


def responde(cuerpo: dict) -> dict:
    mensajes = cuerpo.get("messages") or []
    texto = "\n".join(str(m.get("content") or "") for m in mensajes)
    ultimo = next((str(m.get("content") or "") for m in reversed(mensajes)
                   if m.get("role") == "user"), "")

    with CERROJO:
        ESTADO["peticiones"] += 1
        orden = ESTADO["peticiones"]
        for etiqueta, nonce in RE_CODIGO.findall(texto):
            ESTADO["vistos"][etiqueta] = nonce
        vistos = dict(ESTADO["vistos"])
        firma = hashlib.sha256(texto.encode("utf-8")).hexdigest()
        acierto = bool(cuerpo.get("cache_prompt")) and firma in ESTADO["prefijos"]
        if cuerpo.get("cache_prompt"):
            ESTADO["prefijos"].add(firma)

    m = RE_PN.search(texto)
    prompt_n = int(m.group(1)) if m else max(1, len(texto) // 4)

    # H-034: caida de la GPU simulada en el prefill largo (#27306).
    umbral = CONF.get("caida_en_ctx")
    if umbral is not None and prompt_n >= int(umbral):
        os._exit(1)

    pp = float(por_brazo("pp", 300.0))
    if orden >= 2:
        pp *= float(CONF.get("pp_factor_desde_2", 1.0))
    tg = float(por_brazo("tg", 25.0))

    mtp = mtp_activo() and not CONF.get("mtp_sin_efecto")
    if mtp:
        tg *= float(CONF.get("mtp_factor", 1.0))
        # H-034: "una familia se hunde" — factor por familia, sobre el de MTP.
        por_familia = CONF.get("mtp_tg_por_familia") or {}
        fam = familia_de(texto)
        if fam and fam in por_familia:
            tg *= float(por_familia[fam])

    if acierto:
        cache = CONF.get("prompt_ms_cache", 120.0)
        if isinstance(cache, dict):
            cache = cache.get(str(ARGS.get("cache_ram")), cache.get("por_defecto", 120.0))
        prompt_ms = float(cache)
        cache_n = prompt_n
        prompt_n = max(1, prompt_n - cache_n) if cache_n < prompt_n else 4
    else:
        prompt_ms = float(CONF.get("prompt_ms_frio", 900.0))
        cache_n = 0

    # H-034 vision: la imagen manda sobre cualquier otra logica de contenido.
    if _tiene_imagen(mensajes):
        if mtp and CONF.get("vision_mtp_rompe"):
            return {"_http": 500,
                    "error": {"message": "vision rota con la cabeza MTP"}}
        contenido = "Rojo"
    else:
        codigos = RE_CODIGO.findall(texto)
        if "¿Cuál era el CÓDIGO?" in ultimo and codigos:
            etiqueta_propia, nonce_propio = codigos[-1]
            contenido = nonce_propio
            # H-035: la contaminacion puede depender de la cabeza MTP (#28286):
            # `contamina_con_mtp` solo fuga cuando el arranque lleva borrador.
            if CONF.get("contamina") or (mtp and CONF.get("contamina_con_mtp")):
                ajenos = [v for k, v in vistos.items() if k != etiqueta_propia]
                if ajenos:
                    contenido = ajenos[orden % len(ajenos)]
        elif "palabra CUATRO" in ultimo:
            contenido = "CUATRO"
        elif cuerpo.get("seed") is not None:
            contenido = f"{por_brazo('greedy_prefijo', 'G')}{len(ultimo)}"
        else:
            contenido = "OK"
        if mtp and CONF.get("mtp_greedy_distinto"):
            contenido = contenido + "!"

    # H-035: texto largo determinista por familia para poder localizar una
    # divergencia en una posicion concreta con logprobs.
    fam = familia_de(texto)
    if cuerpo.get("logprobs") and fam:
        contenido = _texto_largo(fam)
    lp_conf = CONF.get("divergencia") or {}
    # H-035 fase 3: `divergencia_intra_slot` hace que UNA de cada dos
    # peticiones iguales (orden par) diverja en ambos brazos, como hace el
    # servidor real a np=2. Se clasifica con la misma clase.
    intra = CONF.get("divergencia_intra_slot")
    div = None
    if mtp and fam in lp_conf:
        div = lp_conf[fam]
    elif intra and fam and orden % 2 == 0:
        div = intra
    tokens = None
    if cuerpo.get("logprobs"):
        tokens = _tokens_logprobs(contenido, div)
        if div:
            contenido = "".join(t["token"] for t in tokens)

    predicted = max(1, min(int(cuerpo.get("max_tokens") or 16), len(contenido) // 2 + 1))
    timings = {
        "prompt_n": prompt_n, "prompt_ms": prompt_ms,
        "prompt_per_second": pp, "predicted_n": predicted,
        "predicted_ms": round(predicted / tg * 1000, 3),
        "predicted_per_second": tg, "cache_n": cache_n,
    }
    if mtp:
        acceptance = float(CONF.get("acceptance", 0.66))
        draft_n = int(CONF.get("draft_n", 50))
        timings["draft_n"] = draft_n
        timings["draft_n_accepted"] = int(round(draft_n * acceptance))
    eleccion = {"message": {"content": contenido}, "finish_reason": "stop"}
    if tokens is not None and not CONF.get("sin_logprobs"):
        eleccion["logprobs"] = {"content": tokens}
    return {
        "choices": [eleccion],
        "timings": timings,
    }


def main(argv: list[str]) -> int:
    global CONF, ARGS, CLAVE, BRAZO
    CONF = lee_conf()
    # .../<sha>/build/bin/llama-server -> <sha>
    partes = os.path.abspath(argv[0] if argv else sys.argv[0]).split(os.sep)
    BRAZO = partes[-4] if len(partes) >= 4 else "por_defecto"

    resto = sys.argv[1:]
    if "--help" in resto or "-h" in resto:
        print(AYUDA.format(extra=EXTRA_ENV if CONF.get("soporta_env_api_key") else ""))
        return 0

    ARGS = parsea(resto)
    origen = None
    if os.environ.get("LLAMA_ARG_API_KEY"):
        CLAVE, origen = os.environ["LLAMA_ARG_API_KEY"], "entorno"
    elif ARGS.get("api_key_file"):
        with open(ARGS["api_key_file"], encoding="utf-8") as f:
            CLAVE, origen = f.read().split("\n")[0].strip(), "fichero"
    elif ARGS.get("api_key"):
        CLAVE, origen = ARGS["api_key"], "argv"
    anota({"evento": "arranque", "args": resto, "origen_clave": origen,
           "cache_ram": ARGS.get("cache_ram"), "parallel": ARGS.get("parallel"),
           "kvu": ARGS.get("kvu"), "host": ARGS.get("host"),
           "port": ARGS.get("port"),
           "spec_type": ARGS.get("spec_type"),
           "draft_model": ARGS.get("draft_model"),
           "spec_draft_n_max": ARGS.get("spec_draft_n_max")})

    if CONF.get("morir"):
        return int(CONF["morir"])

    # H-034: la telemetria de borrador que la fase busca en el log del proceso.
    if mtp_activo() and not CONF.get("mtp_sin_efecto"):
        print(f"draft acceptance = {float(CONF.get('acceptance', 0.66)):.4f}",
              flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.get("port", 0)), Manejador)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
