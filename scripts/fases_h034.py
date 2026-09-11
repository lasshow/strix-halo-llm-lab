#!/usr/bin/env python3
"""Fases de H-034: cabeza MTP sidecar, un solo slot, escalera de contexto.

El candidato es la baseline de H-033 (df03399 + PR #28501) con la PR **#28243**
(soporte qwen4exp-MTP) apilada encima, y la cabeza `MTP/shared-Q8_0` de Unsloth
como modelo de borrador. Solo el candidato entiende `--spec-type draft-mtp
-md <cabeza>`; en la baseline esos flags no existen y el servidor ni arranca,
que es exactamente lo que debe pasar si alguien mezcla los brazos.

Por que TODO va con `-np 1` y sin `-kvu`
----------------------------------------
Por el issue **#28286**: `draft-mtp` con `--parallel > 1` contamina slots. H-034
no es la fase para averiguar si eso nos pasa a nosotros -- eso es H-035 --, asi
que aqui se mide la especulacion con un solo slot y se deja la variable quieta.
La linea productiva lleva `-np 2 -kvu`, luego **esta fase no mide la
configuracion de produccion y no puede desplegar nada**. Ninguna funcion
devuelve `aplicar`, y la nota de la fase 1 lo dice por escrito.

Y por que la escalera de contexto va escalon a escalon mirando `dmesg`
---------------------------------------------------------------------
Por el issue **#27306**: con `draft-mtp`, el driver hace un
`llama_decode(ctx_dft)` tras cada ubatch y en RADV eso ha producido DeviceLost
durante prefill largo. Ya tuvimos un reset de GPU real por prefill largo en
H-014, asi que se mira el anillo del kernel en CADA escalon y se para en el
primer incidente: seguir escalando despues de un reset no mide un techo, mide
otra cosa con la GPU tocada.

Lo que NO se admite: "igual de rapido" sin especulacion
-------------------------------------------------------
La confirmacion de que hay especulacion es que `timings` traiga `draft_n` /
`draft_n_accepted` (llama-server las expone cuando hay borrador) o que el log
del servidor diga `draft acceptance = ...`. Si no aparece ninguna de las dos,
la especulacion NO esta activa y la fase reporta **error**: medir dos brazos
identicos y anotar "no mejora" seria publicar una conclusion sobre MTP sin
haber ejecutado MTP.

Nota de Unsloth, para no confundir ruido con fallo: una cabeza `shared-` loguea
UNA linea de error al arrancar (`borrow_shared_tensor: this model is a draft
head`) y despues funciona. No es un fallo y no se cuenta como tal.

UMBRALES, escritos antes de medir
---------------------------------
`mtp_np1_velocidad`
    ADOPTAR si y solo si se cumplen las cinco cosas a la vez:
      1. la mediana de `tg` en **prosa+codigo+reescritura** es **>= 1,15x** la
         del control para al menos uno de los dos `n-max`;
      2. **ninguna** de las cinco familias baja de **0,95x** el control en ese
         brazo (una mejora de media que hunde una familia no es una mejora);
      3. `pp` **>= 0,97x** el control: la cabeza ocupa memoria y toca el
         prefill, y se admite un 3 % de peaje, no mas;
      4. **igualdad greedy exacta**: el `content` de cada brazo `mtp-*` es
         IDENTICO al del control para la misma familia y pasada. La
         verificacion de MTP es exacta por construccion; si el texto cambia, la
         especulacion no esta verificando y no es la misma salida mas rapida;
      5. **especulacion confirmada activa** en los brazos `mtp-*`.
    Si (4) o (5) fallan es `error` + `adoptar=False`, no un brazo peor.

`mtp_escalera_contexto`
    ADOPTAR solo si los CUATRO puntos (8.192 / 32.768 / 65.536 / `H034_CTX_MAX`)
    pasan **sin incidente de GPU**, con el servidor vivo y `/health` en 200
    despues de cada uno, y con `finish_reason` en `stop`/`length`.
    Ademas, donde hay linea base medida en H-033 (**428 t/s a 8k, 340 t/s a
    32k**) se exige `pp` **>= 0,90x** de esa cifra. Para 64k y 98k no hay linea
    base y NO se fabrica una: medir el control en la misma fase doblaria el
    tiempo de la escalera, asi que ahi el criterio es solo "sin incidente y
    terminacion normal", y queda declarado como tal.
    `resumen.techo_ctx` es el ultimo punto sano; tras un incidente no se sigue
    escalando.

`mtp_vision`
    ADOPTAR solo si el control **y** el candidato con MTP responden "rojo"/"red"
    (sin distinguir mayusculas) con `finish_reason=stop` a la misma imagen. Si
    el candidato falla o el servidor se muere con `-md` + `--mmproj`, el error
    es "MTP + vision incompatible en esta build": la vision es requisito de uso
    en esta maquina, no una variable que se pueda apagar para que MTP luzca.

Parametros por entorno (para acortar en pruebas, nunca para maquillar)
    H034_MTP_HEAD   ruta de la cabeza de borrador
    H034_NMAX       `--spec-draft-n-max` de la escalera y de la vision (2)
    H034_CTX_MAX    cuarto punto de la escalera (98304)
    H034_PASADAS    pasadas por familia y brazo en la fase 1 (2)
    BANCO_PAUSA / BANCO_LIMITE_ARRANQUE / BANCO_GRACIA   (ver banco.py)
    JOURNALCTL / SUDO                                    (ver banco.dmesg_desde)
"""
from __future__ import annotations

import base64
import os
import re
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import banco  # noqa: E402
import salud  # noqa: E402
from prompts import ErrorCorpus  # noqa: E402
from validacion import (ErrorInfraestructura, FalloContrato,  # noqa: E402
                        generacion_medida, respuesta_final, timings)

# La cabeza de borrador de Unsloth. Por entorno porque es una ruta de la
# maquina, no una constante del metodo.
MTP_HEAD = ("/models/gguf/qwen38-flash-next/MTP/"
            "mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf")

# --- brazos ---------------------------------------------------------------
# `control` es la BASELINE (sin MTP) con los mismos `-np 1`, sin `-kvu` y con
# la misma `--cache-ram`: si el control corriera con la linea productiva, la
# comparacion mediria tambien el cambio de slots.
NMAX_POR_BRAZO = {"mtp-nmax2": 2, "mtp-nmax3": 3}
BRAZOS_MTP = ("mtp-nmax2", "mtp-nmax3")
BRAZOS = ("control",) + BRAZOS_MTP
CACHE_RAM_BANCO = 4096
SPEC_P_MIN = 0.0          # 0.0 = no se poda por probabilidad: se mide MTP entero

# --- fase 1: velocidad ----------------------------------------------------
FAMILIAS = ("prosa", "codigo", "json", "reescritura", "creativo")
# Las tres donde MTP tiene algo que decir: texto predecible. `json` y
# `creativo` entran como guardia (ninguna familia puede hundirse), no en la
# mediana del umbral.
FAMILIAS_VELOCIDAD = ("prosa", "codigo", "reescritura")
PASADAS = 2
MAX_TOKENS = 256
CORPUS_FAMILIAS = 2048
# ~600 tokens de corpus para la reescritura. Es una aproximacion a 4 chars por
# token y no se finge otra cosa: lo unico que el metodo exige es que el bloque
# sea EL MISMO para los tres brazos, y lo es (sale del corpus congelado).
CHARS_REESCRITURA = 2400

UMBRAL_TG = 1.15          # mediana de prosa+codigo+reescritura vs control
UMBRAL_TG_FAMILIA = 0.95  # suelo por familia: ninguna puede hundirse
UMBRAL_PP = 0.97          # el prefill puede pagar un 3 %, no mas

PROMPT_PROSA = ("Resume en tres frases el texto siguiente. No anadas nada mas "
                "que el resumen.\n\n")
PROMPT_CODIGO = (
    "Escribe una funcion Python `media_por_zona(lecturas)` que reciba una lista "
    "de diccionarios {'zona': str, 'grados': float} y devuelva un diccionario "
    "zona -> media redondeada a un decimal. Anade tres pruebas con `unittest`. "
    "Solo codigo, sin explicaciones.")
PROMPT_JSON = (
    "Transforma esta lista en JSON estricto, un objeto por linea de entrada, "
    "con las claves \"zona\", \"ciclo\" y \"kw\" y sin ningun texto alrededor:\n"
    "zona norte, ciclo 12, 4,7 kW\n"
    "zona sur, ciclo 12, 3,9 kW\n"
    "zona este, ciclo 13, 5,1 kW\n"
    "zona oeste, ciclo 13, 2,8 kW")
PROMPT_REESCRITURA = (
    "Devuelve el bloque siguiente con un unico cambio: pon en mayuscula la "
    "primera letra de cada frase. No reescribas, no resumas y no comentes "
    "nada.\n\n")
PROMPT_CREATIVO = (
    "Escribe el arranque de un relato breve sobre un horno industrial que "
    "aprende a medirse a si mismo. Maximo un parrafo.")

# `llama-server` expone la telemetria de borrador en `timings`; el log lo dice
# ademas en texto. Valen las dos, y hace falta al menos una.
RE_ACEPTACION = re.compile(r"draft\s+acceptance\s*=", re.IGNORECASE)

# --- fase 2: escalera de contexto -----------------------------------------
PUNTOS_ESCALERA = (8192, 32768, 65536)
CTX_MAX = 98304
NMAX_ESCALERA = 2
MAX_TOKENS_ESCALERA = 128
# Medido en H-033 con la baseline y la misma linea productiva. No se vuelve a
# medir aqui: doblar la escalera para tener control propio a 64k/98k cuesta mas
# tiempo del que vale, y por eso a esos dos puntos NO se les exige pp.
PP_BASELINE_H033 = {8192: 428.0, 32768: 340.0}
UMBRAL_PP_ESCALERA = 0.90
RE_INCIDENTE_GPU = re.compile(
    r"amdgpu.*(timeout|reset|ring|GPU reset|DeviceLost)", re.IGNORECASE)
PROMPT_CORTO = "Responde unicamente con la palabra OK."

# --- fase 3: vision -------------------------------------------------------
LADO_PNG = 64
BORDE_PNG = 16
PREGUNTA_VISION = "¿De qué color es el cuadrado? Responde una palabra."
MAX_TOKENS_VISION = 16
COLORES_VISION = ("rojo", "red")


# ===================================================================== comun
def lee_unidad_de(ctx) -> str:
    """Texto de la unidad productiva. Aislado para sustituirlo en pruebas."""
    import campana
    return campana.lee_unidad(ctx.unidad)


def _args_productivos(ctx) -> list[str]:
    return banco.args_de_unidad(lee_unidad_de(ctx))


def _args_banco(base: list[str]) -> list[str]:
    """La linea comun a los tres brazos: un slot, sin `-kvu`, cache reducida."""
    return banco.con_cambios(base, parallel=1, kvu=False,
                             cache_ram=CACHE_RAM_BANCO)


def args_mtp(base: list[str], n_max: int, cabeza: str) -> list[str]:
    """La linea del brazo MTP: la comun + la cabeza sidecar.

    Publica a proposito: el plan de pruebas y las pruebas citan exactamente
    esta linea, y no debe haber una segunda forma de construirla.
    """
    return banco.con_cambios(_args_banco(base), spec_type="draft-mtp",
                             draft_model=cabeza, spec_draft_n_max=n_max,
                             spec_draft_p_min=SPEC_P_MIN)


def _cabeza() -> str:
    return os.environ.get("H034_MTP_HEAD", MTP_HEAD)


def _servidor(ctx, args, etiqueta, brazo):
    return banco.ServidorBanco(
        ctx.binario(brazo), args, ctx.puerto_banco, modelo=ctx.modelo,
        clave=banco.clave_de(ctx), entorno=ctx.entorno_con_clave(),
        log=ctx.log, dir_log=ctx.dir_salida, etiqueta=etiqueta)


def _sin_pensar(**extra) -> dict:
    cuerpo = {"temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    cuerpo.update(extra)
    return cuerpo


def _borrador(d: dict) -> dict:
    """`draft_n` / `draft_n_accepted` de una respuesta, y su acceptance.

    Ausentes -> `{"draft_n": None, ...}`: es "no hay telemetria de borrador",
    no "se especularon cero tokens". La diferencia es la que separa reportar un
    error de publicar una medida en falso.
    """
    t = timings(d)
    dn, da = t.get("draft_n"), t.get("draft_n_accepted")
    ok = (isinstance(dn, (int, float)) and not isinstance(dn, bool) and dn > 0)
    acc = None
    if ok and isinstance(da, (int, float)) and not isinstance(da, bool):
        acc = round(float(da) / float(dn), 4)
    return {"draft_n": int(dn) if ok else None,
            "draft_n_accepted": int(da) if (ok and da is not None) else None,
            "acceptance": acc, "hay_telemetria": bool(ok)}


def _dice_aceptacion(srv) -> bool:
    return any(RE_ACEPTACION.search(l) for l in srv.log_tail(400))


def textos_familias(texto_corpus: str) -> dict:
    """Las cinco familias de prompt, construidas sobre el corpus congelado.

    Deterministas y publicas: los tres brazos reciben EXACTAMENTE el mismo
    texto, que es la unica forma de que la comparacion de contenido greedy
    signifique algo.
    """
    return {
        "prosa": PROMPT_PROSA + texto_corpus,
        "codigo": PROMPT_CODIGO,
        "json": PROMPT_JSON,
        "reescritura": PROMPT_REESCRITURA + texto_corpus[:CHARS_REESCRITURA],
        "creativo": PROMPT_CREATIVO,
    }


# ====================================================== 1. velocidad a np=1
def mtp_np1_velocidad(ctx) -> dict:
    """A/B/C alternado: control, `n-max 2` y `n-max 3`, cinco familias, 2 pasadas.

    Orden real de ejecucion: control, mtp2, mtp3, control, mtp2, mtp3. Alternar
    por ronda reparte entre los tres brazos la deriva termica de la maquina, que
    en una hora es real; medir un brazo entero y despues otro se la regala al
    ultimo (mismo motivo que en H-033).

    Las cinco familias se eligen porque MTP no acelera igual todo: en texto
    predecible (prosa, codigo, y sobre todo REESCRITURA, que es copiar con
    cambios minimos) el borrador acierta casi siempre; en creativo, casi nunca.
    Por eso el umbral se calcula sobre las tres primeras y las otras dos entran
    como guardia: ninguna familia puede caer por debajo de 0,95x.

    No devuelve `aplicar`: `np=1` no es la configuracion productiva. Adoptar
    aqui significa "la build MTP es segura y rapida a un slot", y el despliegue
    con `np=2` es H-035.
    """
    cabeza = _cabeza()
    pasadas = int(os.environ.get("H034_PASADAS", PASADAS))
    corpus = ctx.corpus(CORPUS_FAMILIAS)
    textos = textos_familias(corpus["texto"])
    clave = banco.clave_de(ctx)
    base = _args_productivos(ctx)

    medidas = {b: {f: {"tg": [], "pp": []} for f in FAMILIAS} for b in BRAZOS}
    contenidos: dict[tuple[str, str, int], str] = {}
    aceptaciones = {b: [] for b in BRAZOS_MTP}
    espec = {b: {"timings": False, "log": False} for b in BRAZOS_MTP}
    orden: list[str] = []
    errores: list[str] = []

    for pasada in range(1, pasadas + 1):
        for brazo in BRAZOS:
            orden.append(brazo)
            args = (_args_banco(base) if brazo == "control"
                    else args_mtp(base, NMAX_POR_BRAZO[brazo], cabeza))
            try:
                with _servidor(ctx, args, f"h034-{brazo}-p{pasada}",
                               "baseline" if brazo == "control" else "candidato") as srv:
                    for familia in FAMILIAS:
                        d = banco.peticion_chat(
                            srv.url, clave,
                            [{"role": "user", "content": textos[familia]}],
                            **_sin_pensar(max_tokens=MAX_TOKENS, cache_prompt=False))
                        m = generacion_medida(d, MAX_TOKENS)
                        bo = _borrador(d)
                        medidas[brazo][familia]["tg"].append(m["tg"])
                        medidas[brazo][familia]["pp"].append(m["pp"])
                        contenidos[(brazo, familia, pasada)] = m["texto"]
                        if brazo in BRAZOS_MTP:
                            if bo["hay_telemetria"]:
                                espec[brazo]["timings"] = True
                            if bo["acceptance"] is not None:
                                aceptaciones[brazo].append(bo["acceptance"])
                        ctx.medida({
                            "fase": "h034-velocidad", "brazo": brazo,
                            "config": f"{familia} np1", "familia": familia,
                            "pasada": pasada, "prompt_n": int(m["prompt_n"]),
                            "predicted_n": int(m["predicted_n"]),
                            "prompt_ms": banco.ttft_ms(d),
                            "timings": timings(d), "pp": m["pp"], "tg": m["tg"],
                            "borrador": bo,
                            "esperado": f"tg de {brazo} en {familia}",
                            "obtenido": {"tg": round(m["tg"], 2),
                                         "pp": round(m["pp"], 2),
                                         "acceptance": bo["acceptance"]},
                        })
                    if brazo in BRAZOS_MTP and _dice_aceptacion(srv):
                        espec[brazo]["log"] = True
            except ErrorInfraestructura as e:
                errores.append(f"{brazo} pasada {pasada}: {e}")

    return _veredicto_velocidad(ctx, corpus, cabeza, pasadas, orden, medidas,
                                contenidos, aceptaciones, espec, errores)


def _veredicto_velocidad(ctx, corpus, cabeza, pasadas, orden, medidas,
                         contenidos, aceptaciones, espec, errores) -> dict:
    """Todo el juicio de la fase 1, separado de la toma de datos."""
    resumen = {
        "brazos": list(BRAZOS), "pasadas": pasadas, "orden": orden,
        "familias": list(FAMILIAS),
        "familias_del_umbral": list(FAMILIAS_VELOCIDAD),
        "max_tokens": MAX_TOKENS, "np": 1, "kvu": False,
        "cache_ram": CACHE_RAM_BANCO,
        "cabeza_mtp": os.path.basename(cabeza),
        "corpus": {"objetivo": CORPUS_FAMILIAS, "sha256": corpus["sha256"][:12]},
        "umbrales": {"tg_mediana": UMBRAL_TG, "tg_por_familia": UMBRAL_TG_FAMILIA,
                     "pp": UMBRAL_PP,
                     "greedy": "content identico al control",
                     "especulacion": "draft_n en timings o 'draft acceptance' en el log"},
        "nota": ("adoptar = la build MTP es segura y rapida a np=1; el "
                 "despliegue con np=2 es H-035"),
        "tabla": {}, "brazos_mtp": {},
    }
    for brazo in BRAZOS:
        resumen["tabla"][brazo] = {
            f: {"tg": banco.mediana(medidas[brazo][f]["tg"]),
                "pp": banco.mediana(medidas[brazo][f]["pp"]),
                "muestras": len(medidas[brazo][f]["tg"])}
            for f in FAMILIAS}

    fallos: list[str] = []

    # --- igualdad greedy: exacta, y por familia y pasada ---
    diferencias = []
    for (brazo, familia, pasada), texto in sorted(contenidos.items()):
        if brazo == "control":
            continue
        ref = contenidos.get(("control", familia, pasada))
        if ref is None:
            continue
        if texto != ref:
            diferencias.append({"brazo": brazo, "familia": familia,
                                "pasada": pasada, "control": ref[:80],
                                "mtp": texto[:80]})
    resumen["greedy"] = {
        "comparaciones": sum(1 for (b, f, p) in contenidos
                             if b != "control" and ("control", f, p) in contenidos),
        "identico": not diferencias if contenidos else None,
        "diferencias": diferencias[:6],
    }
    if diferencias:
        d0 = diferencias[0]
        fallos.append(
            f"la salida greedy difiere en {len(diferencias)} comparaciones "
            f"(la primera, {d0['brazo']} familia {d0['familia']} pasada "
            f"{d0['pasada']}: control {d0['control']!r} vs mtp {d0['mtp']!r}); "
            "la verificacion de MTP es exacta, asi que un texto distinto "
            "significa que la especulacion no esta verificando")

    # --- ¿especulo de verdad? ---
    for brazo in BRAZOS_MTP:
        activa = espec[brazo]["timings"] or espec[brazo]["log"]
        resumen["brazos_mtp"][brazo] = {
            "n_max": NMAX_POR_BRAZO[brazo],
            "especulacion_activa": activa,
            "por_timings": espec[brazo]["timings"],
            "por_log": espec[brazo]["log"],
            "acceptance_mediana": banco.mediana(aceptaciones[brazo]),
            "acceptance_muestras": len(aceptaciones[brazo]),
        }
        if not activa and not errores:
            fallos.append(
                f"{brazo}: la especulacion NO esta activa (ni `draft_n` en "
                "`timings` ni `draft acceptance` en el log del servidor); lo "
                "que se ha medido no es MTP, asi que no se publica como "
                "'igual de rapido'")

    # --- velocidad, brazo a brazo ---
    ctrl_umbral = banco.mediana(
        [v for f in FAMILIAS_VELOCIDAD for v in medidas["control"][f]["tg"]])
    ctrl_pp = banco.mediana(
        [v for f in FAMILIAS for v in medidas["control"][f]["pp"]])
    candidatos = []
    for brazo in BRAZOS_MTP:
        r = resumen["brazos_mtp"][brazo]
        tg = banco.mediana(
            [v for f in FAMILIAS_VELOCIDAD for v in medidas[brazo][f]["tg"]])
        pp = banco.mediana([v for f in FAMILIAS for v in medidas[brazo][f]["pp"]])
        r["tg_mediana"] = tg
        r["ratio_tg"] = round(tg / ctrl_umbral, 4) if (tg and ctrl_umbral) else None
        r["ratio_pp"] = round(pp / ctrl_pp, 4) if (pp and ctrl_pp) else None
        r["ratio_tg_por_familia"] = {}
        motivos = []
        for f in FAMILIAS:
            c = resumen["tabla"]["control"][f]["tg"]
            v = resumen["tabla"][brazo][f]["tg"]
            ratio = round(v / c, 4) if (c and v) else None
            r["ratio_tg_por_familia"][f] = ratio
            if ratio is None:
                motivos.append(f"{f}: falta la medida de algun brazo")
            elif ratio < UMBRAL_TG_FAMILIA:
                motivos.append(f"{f}: tg {ratio:.3f}x, por debajo de "
                               f"{UMBRAL_TG_FAMILIA}")
        if r["ratio_tg"] is None:
            motivos.append("sin mediana de tg comparable")
        elif r["ratio_tg"] < UMBRAL_TG:
            motivos.append(f"tg mediana {r['ratio_tg']:.3f}x, por debajo de "
                           f"{UMBRAL_TG}")
        if r["ratio_pp"] is None:
            motivos.append("sin mediana de pp comparable")
        elif r["ratio_pp"] < UMBRAL_PP:
            motivos.append(f"pp {r['ratio_pp']:.3f}x, por debajo de {UMBRAL_PP}")
        r["motivos"] = motivos
        r["pasa"] = not motivos
        if not motivos:
            candidatos.append(brazo)

    # Empate: gana el `n-max` mas bajo. Menos tokens de borrador es menos
    # memoria de cabeza y menos superficie para #28286 en la fase siguiente.
    candidatos.sort(key=lambda b: (-(resumen["brazos_mtp"][b]["ratio_tg"] or 0),
                                   NMAX_POR_BRAZO[b]))
    resumen["nmax_recomendado"] = (NMAX_POR_BRAZO[candidatos[0]]
                                   if candidatos else None)
    resumen["brazo_recomendado"] = candidatos[0] if candidatos else None
    if not candidatos:
        fallos.append("ningun n-max alcanza el umbral de velocidad: " + "; ".join(
            f"{b} ({', '.join(resumen['brazos_mtp'][b]['motivos'])})"
            for b in BRAZOS_MTP))

    resumen["fallos"] = fallos
    resumen["errores"] = errores
    adoptar = not fallos and not errores
    salida = {"nombre": "h034_mtp_np1_velocidad", "resumen": resumen,
              "adoptar": adoptar}
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    ctx.log(f"  H-034 velocidad: adoptar={adoptar} "
            f"n-max recomendado={resumen['nmax_recomendado']}")
    return salida


# ============================================== 2. escalera de contexto
def mtp_escalera_contexto(ctx) -> dict:
    """8k / 32k / 64k / `H034_CTX_MAX` con MTP a un slot, mirando el kernel.

    Antes de gastar una escalera entera se comprueba con UNA peticion corta que
    la especulacion esta activa: si la fase 1 fallo por eso, aqui no tiene
    sentido medir cuatro contextos de un servidor que no esta especulando. Sale
    con `adoptar=False` y el motivo escrito, sin escalar.

    En cada escalon, y en este orden: peticion fria -> `dmesg` desde el inicio
    de la fase -> `/health` sigue en 200 -> una peticion corta responde. Un
    incidente de GPU o un servidor muerto fijan `techo_ctx` en el ultimo punto
    sano y **paran la escalera**; seguir subiendo despues de un reset no mide
    un techo.

    Si no se puede leer el anillo del kernel, el punto se anota como "sin
    vigilancia de kernel". Eso NO es un punto sano: es un punto sin mirar.
    """
    cabeza = _cabeza()
    n_max = int(os.environ.get("H034_NMAX", NMAX_ESCALERA))
    ctx_max = int(os.environ.get("H034_CTX_MAX", CTX_MAX))
    puntos = list(PUNTOS_ESCALERA) + [ctx_max]
    corpus = _corpus_de_la_escalera(ctx, puntos)

    clave = banco.clave_de(ctx)
    args = args_mtp(_args_productivos(ctx), n_max, cabeza)
    marca = banco.marca_ahora()

    resumen = {
        "puntos": puntos, "n_max": n_max, "np": 1, "kvu": False,
        "max_tokens": MAX_TOKENS_ESCALERA,
        "cabeza_mtp": os.path.basename(cabeza),
        "marca_kernel": marca,
        "pp_baseline_h033": {str(k): v for k, v in PP_BASELINE_H033.items()},
        "umbral_pp": UMBRAL_PP_ESCALERA,
        "criterio": (f"los {len(puntos)} puntos sin incidente de GPU, con "
                     f"/health 200 y finish_reason stop/length; y pp >= "
                     f"{UMBRAL_PP_ESCALERA}x la baseline de H-033 donde la hay "
                     f"({', '.join(f'{k}:{v}' for k, v in PP_BASELINE_H033.items())})"),
        "corpus": {str(t): corpus[t]["sha256"][:12] for t in puntos},
        "escalones": {}, "techo_ctx": None,
    }
    fallos: list[str] = []
    errores: list[str] = []

    try:
        with _servidor(ctx, args, f"h034-escalera-nmax{n_max}", "candidato") as srv:
            activa, detalle = _confirma_especulacion(ctx, srv, clave)
            resumen["especulacion"] = detalle
            if not activa:
                fallos.append(
                    "la especulacion no esta activa (ni `draft_n` en `timings` "
                    "ni `draft acceptance` en el log): no escalo el contexto de "
                    "un servidor que no esta especulando")
            else:
                _sube_la_escalera(ctx, srv, clave, corpus, puntos, marca,
                                  resumen, fallos)
    except ErrorInfraestructura as e:
        errores.append(f"el servidor de la escalera no se sostuvo: {e}")

    resumen["fallos"] = fallos
    resumen["errores"] = errores
    adoptar = (not fallos and not errores
               and len(resumen["escalones"]) == len(puntos))
    salida = {"nombre": "h034_mtp_escalera_contexto", "resumen": resumen,
              "adoptar": adoptar}
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    ctx.log(f"  H-034 escalera: adoptar={adoptar} techo={resumen['techo_ctx']}")
    return salida


def _corpus_de_la_escalera(ctx, puntos) -> dict:
    """Los cuatro corpus, cargados ANTES de arrancar nada.

    Descubrir a los cuarenta minutos que falta el corpus de 98.304 seria tirar
    la escalera entera, asi que falta uno y la fase aborta aqui con la orden
    exacta que hay que ejecutar. Es `ErrorCorpus`, que es lo que significa: el
    punto no se puede medir, no es que haya medido mal.
    """
    corpus, faltan = {}, []
    for t in puntos:
        try:
            corpus[t] = ctx.corpus(t)
        except ErrorCorpus as e:
            faltan.append((t, str(e)))
    if faltan:
        cuales = " ".join(str(t) for t, _ in faltan)
        raise ErrorCorpus(
            f"la escalera de contexto de H-034 necesita corpus congelado para "
            f"{cuales} y no lo hay: " + "; ".join(m for _, m in faltan) +
            f". Generalos antes de lanzar la fase con: "
            f"python3 prompts.py generar --objetivos {cuales}")
    return corpus


def _confirma_especulacion(ctx, srv, clave) -> tuple[bool, dict]:
    """Una peticion corta que responde a "¿esto especula?" antes de gastar horas."""
    d = banco.peticion_chat(srv.url, clave,
                            [{"role": "user", "content": PROMPT_CORTO}],
                            **_sin_pensar(max_tokens=16, cache_prompt=False))
    bo = _borrador(d)
    por_log = _dice_aceptacion(srv)
    detalle = {"por_timings": bo["hay_telemetria"], "por_log": por_log,
               "acceptance": bo["acceptance"],
               "activa": bool(bo["hay_telemetria"] or por_log)}
    ctx.medida({
        "fase": "h034-escalera", "brazo": "candidato", "config": "sonda",
        "etapa": "confirmacion", "prompt_n": int(timings(d)["prompt_n"]),
        "timings": timings(d), "borrador": bo,
        "esperado": "telemetria de borrador o 'draft acceptance' en el log",
        "obtenido": detalle,
    })
    return detalle["activa"], detalle


def _sube_la_escalera(ctx, srv, clave, corpus, puntos, marca, resumen, fallos):
    """Escalon a escalon, parando en el primer incidente."""
    for t in puntos:
        r = {"objetivo": t}
        try:
            d = banco.peticion_chat(
                srv.url, clave,
                [{"role": "user", "content": corpus[t]["texto"]}],
                **_sin_pensar(max_tokens=MAX_TOKENS_ESCALERA, cache_prompt=False))
            m = generacion_medida(d, MAX_TOKENS_ESCALERA)
            ctx.verifica_corpus(t, int(m["prompt_n"]))
            bo = _borrador(d)
            r.update(prompt_n=int(m["prompt_n"]), pp=m["pp"], tg=m["tg"],
                     predicted_n=int(m["predicted_n"]),
                     finish_reason=m["finish_reason"], borrador=bo)
        except ErrorInfraestructura as e:
            r.update(error=str(e), murio=True)
            resumen["escalones"][str(t)] = r
            fallos.append(f"{t} tokens: el servidor no completo la peticion ({e}); "
                          f"techo de contexto en {resumen['techo_ctx']}")
            ctx.medida({"fase": "h034-escalera", "brazo": "candidato",
                        "config": f"ctx {t}", "esperado": "peticion completada",
                        "obtenido": None, "error": str(e)})
            return

        lineas = banco.dmesg_desde(marca)
        if lineas is None:
            r["vigilancia_kernel"] = False
            r["gpu_incidente"] = None
            r["gpu_lineas"] = []
            fallos.append(f"{t} tokens: no se pudo leer el anillo del kernel "
                          "(sin `journalctl -k`, ni con sudo -n): el punto "
                          "queda SIN VIGILANCIA, que no es lo mismo que sano")
        else:
            incidentes = [l for l in lineas if RE_INCIDENTE_GPU.search(l)]
            r["vigilancia_kernel"] = True
            r["gpu_incidente"] = bool(incidentes)
            r["gpu_lineas"] = incidentes[:8]

        r["vivo"] = srv.esta_vivo()
        r["health_200"] = salud.health_ok(srv.puerto)
        r["responde_corto"] = False
        if r["vivo"] and r["health_200"]:
            try:
                banco.peticion_chat(
                    srv.url, clave, [{"role": "user", "content": PROMPT_CORTO}],
                    **_sin_pensar(max_tokens=16, cache_prompt=False))
                r["responde_corto"] = True
            except ErrorInfraestructura as e:
                r["error_corto"] = str(e)

        if r["finish_reason"] not in ("stop", "length"):
            fallos.append(f"{t} tokens: finish_reason={r['finish_reason']!r}, "
                          "no es una terminacion normal")
        base = PP_BASELINE_H033.get(t)
        if base:
            r["pp_baseline"] = base
            r["ratio_pp"] = round(r["pp"] / base, 4)
            if r["ratio_pp"] < UMBRAL_PP_ESCALERA:
                fallos.append(f"{t} tokens: pp {r['ratio_pp']:.3f}x la baseline "
                              f"de H-033 ({base} t/s), por debajo de "
                              f"{UMBRAL_PP_ESCALERA}")
        else:
            # Sin linea base no se inventa una: se declara y punto.
            r["ratio_pp"] = None
            r["nota_pp"] = ("sin baseline de H-033 a este tamano: solo se exige "
                            "que no haya incidente y que termine normal")

        ctx.medida({
            "fase": "h034-escalera", "brazo": "candidato", "config": f"ctx {t}",
            "tamano": t, "prompt_n": r["prompt_n"], "pp": r["pp"], "tg": r["tg"],
            "timings": None, "borrador": r.get("borrador"),
            "gpu_incidente": r["gpu_incidente"],
            "gpu_lineas": r["gpu_lineas"],
            "esperado": f"prefill de {t} tokens sin reset de GPU",
            "obtenido": {"pp": round(r["pp"], 2), "tg": round(r["tg"], 2),
                         "finish_reason": r["finish_reason"],
                         "health_200": r["health_200"]},
        })
        resumen["escalones"][str(t)] = r

        if r["gpu_incidente"] or not r["vivo"] or not r["health_200"] \
                or not r["responde_corto"]:
            fallos.append(
                f"{t} tokens: incidente tras el prefill "
                f"(gpu_incidente={r['gpu_incidente']}, vivo={r['vivo']}, "
                f"health_200={r['health_200']}, responde={r['responde_corto']}); "
                f"techo de contexto en {resumen['techo_ctx']}, no sigo escalando")
            return
        resumen["techo_ctx"] = t


# ===================================================================== 3. vision
def png_cuadrado_rojo(lado: int = LADO_PNG, borde: int = BORDE_PNG) -> bytes:
    """Un PNG RGB de `lado`x`lado`: cuadrado rojo centrado sobre blanco.

    A mano con `zlib` + `struct` y sin PIL a proposito: la imagen de prueba no
    puede depender de que este instalada una biblioteca de imagen en la maquina
    que ejecuta la campana, y un PNG sin comprimir de 64x64 son cuatro lineas.
    """
    filas = []
    for y in range(lado):
        fila = bytearray([0])                       # filtro 0 (None) por fila
        for x in range(lado):
            dentro = borde <= x < lado - borde and borde <= y < lado - borde
            fila += bytes((255, 0, 0) if dentro else (255, 255, 255))
        filas.append(bytes(fila))

    def trozo(tipo: bytes, datos: bytes) -> bytes:
        return (struct.pack(">I", len(datos)) + tipo + datos +
                struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF))

    cabecera = struct.pack(">IIBBBBB", lado, lado, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + trozo(b"IHDR", cabecera) +
            trozo(b"IDAT", zlib.compress(b"".join(filas), 9)) +
            trozo(b"IEND", b""))


def mensajes_vision() -> list[dict]:
    url = "data:image/png;base64," + base64.b64encode(
        png_cuadrado_rojo()).decode("ascii")
    return [{"role": "user", "content": [
        {"type": "text", "text": PREGUNTA_VISION},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]


def mtp_vision(ctx) -> dict:
    """¿Sobrevive la vision a la cabeza de borrador? Control y candidato, misma imagen.

    La vision es requisito de uso en esta maquina (`--mmproj` esta en la linea
    productiva), no una variable que se pueda apagar para que MTP luzca. Asi que
    la pregunta no es "¿cuanto cuesta?" sino "¿funciona?": una imagen minima
    generada aqui mismo, una palabra de respuesta y el mismo contrato para los
    dos brazos.

    Los DOS tienen que acertar. Si falla solo el candidato -- o si el servidor se
    muere con `-md` + `--mmproj` --, el veredicto es "MTP + vision incompatible
    en esta build". Si falla tambien el control, el problema no es de MTP y la
    prueba no concluye nada sobre el: tambien sale `adoptar=False`, pero con
    otro motivo escrito.
    """
    cabeza = _cabeza()
    n_max = int(os.environ.get("H034_NMAX", NMAX_ESCALERA))
    clave = banco.clave_de(ctx)
    base = _args_productivos(ctx)
    mmproj = banco.valor_de(base, "mmproj")

    resumen = {
        "pregunta": PREGUNTA_VISION, "max_tokens": MAX_TOKENS_VISION,
        "colores_aceptados": list(COLORES_VISION),
        "imagen": {"formato": "PNG", "lado": LADO_PNG, "borde": BORDE_PNG,
                   "bytes": len(png_cuadrado_rojo())},
        "mmproj": mmproj, "n_max": n_max, "np": 1,
        "cabeza_mtp": os.path.basename(cabeza),
        "criterio": ("control y candidato responden rojo/red con "
                     "finish_reason=stop"),
        "brazos": {},
    }
    if not mmproj:
        return {"nombre": "h034_mtp_vision", "resumen": resumen, "adoptar": False,
                "error": ("la linea productiva no trae `--mmproj`: esta fase no "
                          "puede medir vision y no da por buena la combinacion")}

    mensajes = mensajes_vision()
    plan = (("control", "baseline", _args_banco(base)),
            ("candidato-mtp", "candidato", args_mtp(base, n_max, cabeza)))
    for brazo, build, args in plan:
        r = {"acierta": False}
        try:
            with _servidor(ctx, args, f"h034-vision-{brazo}", build) as srv:
                d = banco.peticion_chat(
                    srv.url, clave, mensajes,
                    **_sin_pensar(max_tokens=MAX_TOKENS_VISION, cache_prompt=False))
                texto = respuesta_final(d, exigir_stop=True)
                r["texto"] = texto[:200]
                r["acierta"] = any(c in texto.lower() for c in COLORES_VISION)
                if not r["acierta"]:
                    r["error"] = (f"la respuesta no nombra el color: {texto[:80]!r} "
                                  f"(se esperaba una de {COLORES_VISION})")
        except FalloContrato as e:
            r["error"] = f"contrato: {e}"
        except ErrorInfraestructura as e:
            r["error"] = f"infraestructura: {e}"
        resumen["brazos"][brazo] = r
        ctx.medida({
            "fase": "h034-vision", "brazo": brazo, "config": "mmproj + mtp"
            if brazo != "control" else "mmproj",
            "esperado": "/".join(COLORES_VISION),
            "obtenido": r.get("texto"), "acierta": r["acierta"],
            "error": r.get("error"),
        })

    control = resumen["brazos"]["control"]
    candidato = resumen["brazos"]["candidato-mtp"]
    adoptar = bool(control["acierta"] and candidato["acierta"])
    salida = {"nombre": "h034_mtp_vision", "resumen": resumen, "adoptar": adoptar}
    if not candidato["acierta"]:
        salida["error"] = ("MTP + vision incompatible en esta build: "
                           + candidato.get("error", "el candidato no acerto"))
    elif not control["acierta"]:
        salida["error"] = ("el control tampoco acierta la imagen, asi que esta "
                           "fase no dice nada sobre MTP: "
                           + control.get("error", "el control no acerto"))
    ctx.log(f"  H-034 vision: control={control['acierta']} "
            f"candidato={candidato['acierta']}")
    return salida
