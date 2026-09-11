#!/usr/bin/env python3
"""H-035: la cabeza MTP sidecar en la configuracion PRODUCTIVA (`np=2 -kvu`).

H-034 dejo tres cosas medidas a `np=1`: la cabeza acelera x1,9 donde el texto
es predecible, sube 98k de contexto sin incidente y no rompe la vision. Y una
sin cerrar: en 10 de 20 comparaciones la salida greedy del brazo MTP difiere
del control, y la fase solo guardo 80 caracteres, asi que no se pudo decir
DONDE ni POR QUE. Aqui se cierra eso primero y luego se prueba la combinacion
que toca el issue #28286 (`draft-mtp` con `--parallel > 1` contamina slots).

Fases, en orden:

1. `diagnostico_greedy` (np=1, misma linea que H-034): control y MTP con
   `logprobs: true, top_logprobs: 5`, content COMPLETO. Para cada divergencia
   se localiza el primer token distinto y se mira el logprob del control en
   ese punto: si el token que eligio MTP esta entre los top del control a
   menos de `MARGEN_EMPATE` nats del primero, es un EMPATE NUMERICO (deriva
   del lote de verificacion, benigno: la PR verifica, pero el argmax cambia
   con el batch). Si el token de MTP NO esta en el top-5 del control o esta a
   mas del margen, la PR ACEPTO UN TOKEN QUE EL OBJETIVO NO HABRIA ELEGIDO:
   eso es no verificar, y tumba la adopcion.

2. `np2_kvu_aislamiento`: MTP con `-np 2 -kvu` (la linea productiva). Cuatro
   conversaciones con nonce propio, N ciclos de pares concurrentes (el mismo
   contrato que H-032: `recuperacion_nonce`). Una sola contaminacion = #28286
   nos toca = no se despliega.

3. `np2_kvu_velocidad`: A/B alternado control vs MTP a `np=2 -kvu`, cinco
   familias, dos pasadas, con dos peticiones concurrentes por familia (el
   servidor productivo atiende dos slots; medir uno solo seria medir otra
   cosa). Umbrales: tg mediana (prosa+codigo+reescritura) >= 1,15x; ninguna
   familia < 0,95x; pp >= 0,97x; content greedy identico O explicado como
   empate por la fase 1.

4. `np2_kvu_vision`: cuadrado rojo con `--mmproj` en ambos brazos, y ADEMAS
   una pregunta de vision concurrente con una de texto (los dos slots).

Solo la fase 3 devuelve `aplicar`: si TODAS las fases adoptan, el runner
escribe los flags MTP en la unidad (`banco.pon_mtp`) y promueve la build
candidata (`builds.sh promover`); el gate `restauracion.sh` + `smoke-test.sh`
decide, y si sale rojo revierte unidad y build. Si alguna fase no adopta,
`aplicar` deja la unidad SIN flags MTP (idempotente con la de hoy).

Variables de entorno (solo para ensayos y pruebas):
  H035_MTP_HEAD    ruta de la cabeza (por defecto la de H-034)
  H035_NMAX        n-max a desplegar (por defecto 2: no pierde en ninguna familia)
  H035_CICLOS      ciclos concurrentes de aislamiento (defecto 30)
  H035_PASADAS     pasadas del A/B (defecto 2)
  H035_MARGEN      margen de empate en nats (defecto 0.5)
"""
from __future__ import annotations

import concurrent.futures
import difflib
import os
import random
import secrets
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

import banco  # noqa: E402
import fases_h034 as h034  # noqa: E402
from validacion import (ErrorInfraestructura, FalloContrato,  # noqa: E402
                        generacion_medida, recuperacion_nonce,
                        respuesta_final, timings, tokens_con_logprobs)

# ------------------------------------------------------------- constantes
NMAX_DESPLIEGUE = 2
MARGEN_EMPATE = 0.5        # nats: |lp(control, token_mtp) - lp(control, top1)|
TOP_LOGPROBS = 5
MAX_TOKENS = 256
PASADAS = 2
CICLOS = 30
CONVERSACIONES = ("A", "B", "C", "D")
CORPUS_SIEMBRA = 2048

UMBRAL_TG = h034.UMBRAL_TG
UMBRAL_TG_FAMILIA = h034.UMBRAL_TG_FAMILIA
UMBRAL_PP = h034.UMBRAL_PP
FAMILIAS = h034.FAMILIAS
FAMILIAS_VELOCIDAD = h034.FAMILIAS_VELOCIDAD

PREGUNTA_TEXTO_CONCURRENTE = "Escribe la palabra CUATRO en mayusculas y nada mas."
RESPUESTA_TEXTO_CONCURRENTE = "CUATRO"


def _cabeza() -> str:
    return os.environ.get("H035_MTP_HEAD", os.environ.get("H034_MTP_HEAD", h034.MTP_HEAD))


def _nmax() -> int:
    return int(os.environ.get("H035_NMAX", NMAX_DESPLIEGUE))


def _margen() -> float:
    return float(os.environ.get("H035_MARGEN", MARGEN_EMPATE))


def args_prod_mtp(base: list[str], n_max: int, cabeza: str) -> list[str]:
    """La linea PRODUCTIVA (np, kvu, cache-ram tal cual) + la cabeza sidecar.

    A diferencia de `h034.args_mtp`, aqui no se toca `-np` ni `-kvu`: lo que
    se mide es exactamente lo que se desplegaria.
    """
    return banco.con_cambios(base, spec_type="draft-mtp", draft_model=cabeza,
                             spec_draft_n_max=n_max, spec_draft_p_min=h034.SPEC_P_MIN)


def _servidor(ctx, args, etiqueta, brazo):
    return banco.ServidorBanco(
        ctx.binario(brazo), args, ctx.puerto_banco, modelo=ctx.modelo,
        clave=banco.clave_de(ctx), entorno=ctx.entorno_con_clave(),
        log=ctx.log, dir_log=ctx.dir_salida, etiqueta=etiqueta)


def _sin_pensar(**extra) -> dict:
    return h034._sin_pensar(**extra)


# ============================================ 1. diagnostico de la divergencia
def primer_token_distinto(a: list[dict], b: list[dict]) -> int | None:
    """Indice del primer token en que difieren dos secuencias con logprobs."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x["token"] != y["token"]:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def clasifica_divergencia(control: list[dict], mtp: list[dict],
                          margen: float) -> dict:
    """Decide entre EMPATE NUMERICO y TOKEN NO VERIFICADO en la primera divergencia.

    Se mira la distribucion del CONTROL en la posicion i (hasta ahi los dos
    textos son iguales, asi que el control tambien vio ese contexto):
      - `top1` = token que el control eligio y su logprob.
      - `lp_mtp` = logprob que el control daba al token que eligio MTP.
    Si `lp_mtp` existe y `top1 - lp_mtp <= margen` -> "empate": la PR verifico
    contra una distribucion casi identica y el argmax bailo. Si el token de MTP
    no esta en el top del control o la distancia supera el margen -> "no_verificado".
    Sin divergencia -> "identico".
    """
    i = primer_token_distinto(control, mtp)
    if i is None:
        return {"clase": "identico", "posicion": None}
    if i >= len(control) or i >= len(mtp):
        # Uno se acabo antes: misma secuencia hasta ahi, longitudes distintas.
        return {"clase": "longitud", "posicion": i,
                "len_control": len(control), "len_mtp": len(mtp)}
    c, m = control[i], mtp[i]
    top1_lp = c["logprob"]
    lp_mtp = c["top"].get(m["token"])
    if lp_mtp is None:
        clase, distancia = "no_verificado", None
    else:
        distancia = round(top1_lp - lp_mtp, 4)
        clase = "empate" if distancia <= margen else "no_verificado"
    return {"clase": clase, "posicion": i, "token_control": c["token"],
            "token_mtp": m["token"], "logprob_control_top1": round(top1_lp, 4),
            "logprob_control_del_token_mtp": (round(lp_mtp, 4)
                                             if lp_mtp is not None else None),
            "distancia_nats": distancia, "margen": margen,
            "top_control": {k: round(v, 4) for k, v in c["top"].items()}}


def diagnostico_greedy(ctx) -> dict:
    """np=1, control vs MTP (n-max del despliegue), cinco familias, con logprobs.

    Guarda el content COMPLETO de cada brazo (la carencia de H-034) y, por
    divergencia, la clasificacion. Adopta si TODAS las divergencias son
    empates numericos (o no hay ninguna). Una sola 'no_verificado' tumba
    la fase: la PR estaria aceptando tokens sin verificar.
    """
    cabeza, n_max, margen = _cabeza(), _nmax(), _margen()
    corpus = ctx.corpus(h034.CORPUS_FAMILIAS)
    textos = h034.textos_familias(corpus["texto"])
    clave = banco.clave_de(ctx)
    base = h034._args_productivos(ctx)
    brazos = {"control": h034._args_banco(base),
              "mtp": h034.args_mtp(base, n_max, cabeza)}
    secuencias: dict[tuple[str, str], list[dict]] = {}
    contenidos: dict[tuple[str, str], str] = {}
    errores: list[str] = []
    for brazo, args in brazos.items():
        try:
            with _servidor(ctx, args, f"h035-diag-{brazo}",
                           "baseline" if brazo == "control" else "candidato") as srv:
                for familia in FAMILIAS:
                    d = banco.peticion_chat(
                        srv.url, clave,
                        [{"role": "user", "content": textos[familia]}],
                        **_sin_pensar(max_tokens=MAX_TOKENS, cache_prompt=False,
                                      logprobs=True, top_logprobs=TOP_LOGPROBS))
                    m = generacion_medida(d, MAX_TOKENS)
                    toks = tokens_con_logprobs(d)
                    secuencias[(brazo, familia)] = toks
                    contenidos[(brazo, familia)] = m["texto"]
                    ctx.medida({
                        "fase": "h035-diagnostico", "brazo": brazo,
                        "config": f"{familia} np1 logprobs", "familia": familia,
                        "prompt_n": int(m["prompt_n"]), "predicted_n": int(m["predicted_n"]),
                        "tokens_con_logprobs": len(toks),
                        "timings": timings(d), "borrador": h034._borrador(d),
                        "content_completo": m["texto"],
                        "esperado": "content y logprobs completos",
                        "obtenido": {"chars": len(m["texto"]), "tokens": len(toks)},
                    })
        except ErrorInfraestructura as e:
            errores.append(f"{brazo}: {e}")

    divergencias, fallos = {}, []
    for familia in FAMILIAS:
        c, m = secuencias.get(("control", familia)), secuencias.get(("mtp", familia))
        if c is None or m is None:
            continue
        cl = clasifica_divergencia(c, m, margen)
        cl["content_control"] = contenidos[("control", familia)]
        cl["content_mtp"] = contenidos[("mtp", familia)]
        if cl["clase"] != "identico":
            cl["diff"] = "".join(difflib.unified_diff(
                cl["content_control"].splitlines(True),
                cl["content_mtp"].splitlines(True), "control", "mtp", n=1))[:4000]
        divergencias[familia] = cl
        if cl["clase"] == "no_verificado":
            fallos.append(
                f"{familia}: en el token {cl['posicion']} MTP eligio "
                f"{cl['token_mtp']!r} y el control {cl['token_control']!r}; el "
                f"control daba al token de MTP logprob "
                f"{cl['logprob_control_del_token_mtp']} (top1 "
                f"{cl['logprob_control_top1']}, distancia {cl['distancia_nats']} "
                f"> {margen} nats): la PR acepto un token que el objetivo no elegia")
    clases = {f: d["clase"] for f, d in divergencias.items()}
    resumen = {
        "np": 1, "n_max": n_max, "margen_empate_nats": margen,
        "top_logprobs": TOP_LOGPROBS, "max_tokens": MAX_TOKENS,
        "cabeza_mtp": os.path.basename(cabeza),
        "corpus": {"objetivo": h034.CORPUS_FAMILIAS, "sha256": corpus["sha256"][:12]},
        "criterio": ("toda divergencia greedy debe ser un empate numerico: el "
                     f"token de MTP a <= {margen} nats del top1 del control en la "
                     "primera posicion distinta; 'no_verificado' tumba la fase"),
        "clases": clases,
        "n_identicas": sum(1 for c in clases.values() if c == "identico"),
        "n_empates": sum(1 for c in clases.values() if c == "empate"),
        "n_no_verificadas": sum(1 for c in clases.values() if c == "no_verificado"),
        "n_longitud": sum(1 for c in clases.values() if c == "longitud"),
        "divergencias": divergencias, "fallos": fallos, "errores": errores,
    }
    if len(divergencias) < len(FAMILIAS) and not errores:
        fallos.append(f"solo {len(divergencias)} de {len(FAMILIAS)} familias comparables")
    adoptar = not fallos and not errores
    # Las familias que divergen por EMPATE se le dicen a la fase de velocidad
    # por el contexto compartido: alli una diferencia MTP/control en esas
    # familias no es fallo. Solo si esta fase adopta; si no, no se explica nada.
    ctx.familias_empate_h035 = ({f for f, c in clases.items() if c == "empate"}
                                if adoptar else set())
    salida = {"nombre": "h035_diagnostico_greedy", "resumen": resumen, "adoptar": adoptar}
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    ctx.log(f"  H-035 diagnostico greedy: adoptar={adoptar} clases={clases}")
    return salida


# ============================================ 2. aislamiento de slots (#28286)
def _mensajes_siembra(texto_corpus: str, etiqueta: str, nonce: str) -> list[dict]:
    return [
        {"role": "system",
         "content": "Eres un registro del laboratorio. Guardas un codigo por "
                    "conversacion y lo devuelves tal cual cuando te lo piden."},
        {"role": "user",
         "content": f"{texto_corpus}\n\nCÓDIGO-{etiqueta}: {nonce}\n\n"
                    "Memoriza el código anterior. Responde OK."},
    ]


def _mensajes_pregunta(siembra: list[dict]) -> list[dict]:
    return siembra + [
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "¿Cuál era el CÓDIGO? Responde solo el código."},
    ]


def _pregunta_una(srv, clave, siembra):
    return banco.peticion_chat(srv.url, clave, _mensajes_pregunta(siembra),
                               **_sin_pensar(max_tokens=32, cache_prompt=True))


def _texto(d: dict) -> str:
    ch = d.get("choices") or [{}]
    return ((ch[0].get("message") or {}).get("content") or "").strip()


def np2_kvu_aislamiento(ctx) -> dict:
    """MTP con la linea productiva: cuatro conversaciones, ciclos de dos a la vez.

    Contrato por respuesta (`recuperacion_nonce`): contiene su nonce y ninguno
    ajeno. Una contaminacion = el slot devolvio texto de otro = #28286 nos
    toca = NO se despliega. Un fallo de recuperacion sin contaminacion es peor
    modelo, no fuga, y se cuenta aparte.
    """
    cabeza, n_max = _cabeza(), _nmax()
    ciclos = int(os.environ.get("H035_CICLOS", CICLOS))
    corpus = ctx.corpus(CORPUS_SIEMBRA)
    clave = banco.clave_de(ctx)
    args = args_prod_mtp(h034._args_productivos(ctx), n_max, cabeza)
    rng = random.Random(28286)
    nonces = {et: secrets.token_hex(6) for et in CONVERSACIONES}
    siembras = {et: _mensajes_siembra(corpus["texto"], et, nonces[et])
                for et in CONVERSACIONES}
    contaminaciones = fallos_rec = otros = peticiones = 0
    problemas: list[str] = []
    errores: list[str] = []
    prompt_n_real = None
    espec = {"timings": False, "log": False}
    try:
        with _servidor(ctx, args, "h035-aislamiento", "candidato") as srv:
            for et in CONVERSACIONES:
                d = banco.peticion_chat(srv.url, clave, siembras[et],
                                        **_sin_pensar(max_tokens=16, cache_prompt=True))
                t = timings(d)
                if prompt_n_real is None:
                    prompt_n_real = int(t["prompt_n"])
                ctx.medida({"fase": "h035-aislamiento", "brazo": "candidato",
                            "config": f"np2 kvu nmax{n_max}", "etapa": "siembra",
                            "conversacion": et, "prompt_n": int(t["prompt_n"]),
                            "timings": t, "esperado": "OK", "obtenido": _texto(d)[:80]})
            ctx.verifica_corpus(CORPUS_SIEMBRA, prompt_n_real)
            for ciclo in range(1, ciclos + 1):
                par = rng.sample(CONVERSACIONES, 2)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futuros = {pool.submit(_pregunta_una, srv, clave, siembras[et]): et
                               for et in par}
                    hechos = {futuros[f]: f for f in concurrent.futures.as_completed(futuros)}
                for et in par:
                    peticiones += 1
                    ajenos = [n for e2, n in nonces.items() if e2 != et]
                    reg = {"fase": "h035-aislamiento", "brazo": "candidato",
                           "config": f"np2 kvu nmax{n_max}", "etapa": "ciclo",
                           "ciclo": ciclo, "conversacion": et,
                           "concurrente_con": [e for e in par if e != et],
                           "esperado": nonces[et]}
                    try:
                        d = hechos[et].result()
                    except ErrorInfraestructura as e:
                        otros += 1
                        reg.update(veredicto="infraestructura", obtenido=None, error=str(e))
                        problemas.append(f"ciclo {ciclo} {et}: {e}")
                        ctx.medida(reg)
                        continue
                    bo = h034._borrador(d)
                    if bo["hay_telemetria"]:
                        espec["timings"] = True
                    reg.update(timings=timings(d), borrador=bo, obtenido=_texto(d)[:120])
                    try:
                        recuperacion_nonce(d, nonces[et], ajenos)
                        reg["veredicto"] = "ok"
                    except FalloContrato as e:
                        motivo = getattr(e, "motivo", "contrato")
                        reg.update(veredicto=motivo, error=str(e))
                        if motivo == "contaminacion":
                            contaminaciones += 1
                        elif motivo == "no_recupera":
                            fallos_rec += 1
                        else:
                            otros += 1
                        problemas.append(f"ciclo {ciclo} {et}: {e}")
                    ctx.medida(reg)
            espec["log"] = h034._dice_aceptacion(srv)
    except ErrorInfraestructura as e:
        errores.append(str(e))

    fallos: list[str] = []
    if contaminaciones:
        fallos.append(f"{contaminaciones} contaminacion(es) entre slots en "
                      f"{peticiones} peticiones: #28286 nos toca, no se despliega")
    if peticiones and fallos_rec > peticiones * 0.1:
        fallos.append(f"{fallos_rec} fallos de recuperacion de {peticiones} "
                      "(> 10 %): la cabeza degrada la respuesta bajo concurrencia")
    if not (espec["timings"] or espec["log"]) and not errores:
        fallos.append("la especulacion NO esta activa a np=2: lo medido no es MTP")
    if peticiones == 0 and not errores:
        fallos.append("cero peticiones completadas")
    resumen = {
        "np": 2, "kvu": True, "n_max": n_max, "ciclos": ciclos,
        "cabeza_mtp": os.path.basename(cabeza), "prompt_n": prompt_n_real,
        "peticiones": peticiones, "contaminaciones": contaminaciones,
        "fallos_recuperacion": fallos_rec, "otros_fallos": otros,
        "especulacion": espec, "problemas": problemas[:20],
        "criterio": "cero contaminaciones, <=10 % de fallos de recuperacion, "
                    "especulacion activa",
        "fallos": fallos, "errores": errores,
    }
    adoptar = not fallos and not errores
    salida = {"nombre": "h035_np2_kvu_aislamiento", "resumen": resumen, "adoptar": adoptar}
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    ctx.log(f"  H-035 aislamiento np=2: adoptar={adoptar} "
            f"contaminaciones={contaminaciones}/{peticiones}")
    return salida


# ============================================ 3. velocidad a np=2 -kvu
def _par_concurrente(srv, clave, texto, max_tokens):
    """Dos peticiones iguales a la vez: lo que ve un servidor con dos slots."""
    def una():
        return banco.peticion_chat(srv.url, clave,
                                   [{"role": "user", "content": texto}],
                                   **_sin_pensar(max_tokens=max_tokens, cache_prompt=False))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        return [f.result() for f in [pool.submit(una), pool.submit(una)]]


def np2_kvu_velocidad(ctx) -> dict:
    """A/B alternado control vs MTP a `np=2 -kvu`, dos peticiones a la vez.

    tg y pp se toman de cada respuesta (dos por familia y pasada); el ratio se
    calcula sobre medianas. El content greedy de MTP se compara con el del
    control y, si difiere, se acepta SOLO si el diagnostico (fase 1) clasifico
    esa familia como empate; de lo contrario es fallo. Devuelve `aplicar`.
    """
    cabeza, n_max = _cabeza(), _nmax()
    pasadas = int(os.environ.get("H035_PASADAS", PASADAS))
    corpus = ctx.corpus(h034.CORPUS_FAMILIAS)
    textos = h034.textos_familias(corpus["texto"])
    clave = banco.clave_de(ctx)
    base = h034._args_productivos(ctx)
    brazos = {"control": list(base), "mtp": args_prod_mtp(base, n_max, cabeza)}
    medidas = {b: {f: {"tg": [], "pp": []} for f in FAMILIAS} for b in brazos}
    contenidos: dict[tuple[str, str, int], list[str]] = {}
    aceptaciones: list[float] = []
    espec = {"timings": False, "log": False}
    orden: list[str] = []
    errores: list[str] = []
    for pasada in range(1, pasadas + 1):
        for brazo, args in brazos.items():
            orden.append(brazo)
            try:
                with _servidor(ctx, args, f"h035-vel-{brazo}-p{pasada}",
                               "baseline" if brazo == "control" else "candidato") as srv:
                    for familia in FAMILIAS:
                        par = _par_concurrente(srv, clave, textos[familia], MAX_TOKENS)
                        textos_par = []
                        for k, d in enumerate(par):
                            m = generacion_medida(d, MAX_TOKENS)
                            bo = h034._borrador(d)
                            medidas[brazo][familia]["tg"].append(m["tg"])
                            medidas[brazo][familia]["pp"].append(m["pp"])
                            textos_par.append(m["texto"])
                            if brazo == "mtp":
                                if bo["hay_telemetria"]:
                                    espec["timings"] = True
                                if bo["acceptance"] is not None:
                                    aceptaciones.append(bo["acceptance"])
                            ctx.medida({
                                "fase": "h035-velocidad", "brazo": brazo,
                                "config": f"{familia} np2 kvu", "familia": familia,
                                "pasada": pasada, "slot": k,
                                "prompt_n": int(m["prompt_n"]),
                                "predicted_n": int(m["predicted_n"]),
                                "timings": timings(d), "pp": m["pp"], "tg": m["tg"],
                                "borrador": bo, "content_completo": m["texto"],
                                "esperado": f"tg de {brazo} en {familia}",
                                "obtenido": {"tg": round(m["tg"], 2), "pp": round(m["pp"], 2),
                                             "acceptance": bo["acceptance"]},
                            })
                        contenidos[(brazo, familia, pasada)] = textos_par
                    if brazo == "mtp" and h034._dice_aceptacion(srv):
                        espec["log"] = True
            except ErrorInfraestructura as e:
                errores.append(f"{brazo} pasada {pasada}: {e}")

    fallos: list[str] = []
    tabla = {b: {f: {"tg": banco.mediana(medidas[b][f]["tg"]),
                     "pp": banco.mediana(medidas[b][f]["pp"]),
                     "muestras": len(medidas[b][f]["tg"])} for f in FAMILIAS}
             for b in brazos}
    # greedy: entre slots del mismo brazo (deben ser iguales: mismo prompt) y
    # MTP vs control (igual o empate explicado por la fase 1)
    empates_ok = set(getattr(ctx, "familias_empate_h035", set()))
    greedy = {"intra_slot_distinto": [], "mtp_vs_control_distinto": [],
              "explicadas_por_diagnostico": []}
    for (brazo, familia, pasada), par in sorted(contenidos.items()):
        if len(par) == 2 and par[0] != par[1]:
            greedy["intra_slot_distinto"].append(f"{brazo} {familia} p{pasada}")
        if brazo == "mtp":
            ref = contenidos.get(("control", familia, pasada))
            if ref and par and par[0] != ref[0]:
                if familia in empates_ok:
                    greedy["explicadas_por_diagnostico"].append(f"{familia} p{pasada}")
                else:
                    greedy["mtp_vs_control_distinto"].append(f"{familia} p{pasada}")
    if greedy["intra_slot_distinto"]:
        fallos.append("dos slots con el mismo prompt greedy dieron texto distinto: "
                      + ", ".join(greedy["intra_slot_distinto"]))
    if greedy["mtp_vs_control_distinto"]:
        fallos.append("MTP difiere del control sin que el diagnostico lo explique como "
                      "empate: " + ", ".join(greedy["mtp_vs_control_distinto"]))
    if not (espec["timings"] or espec["log"]) and not errores:
        fallos.append("la especulacion NO esta activa: lo medido no es MTP")

    ctrl_tg = banco.mediana([v for f in FAMILIAS_VELOCIDAD for v in medidas["control"][f]["tg"]])
    mtp_tg = banco.mediana([v for f in FAMILIAS_VELOCIDAD for v in medidas["mtp"][f]["tg"]])
    ctrl_pp = banco.mediana([v for f in FAMILIAS for v in medidas["control"][f]["pp"]])
    mtp_pp = banco.mediana([v for f in FAMILIAS for v in medidas["mtp"][f]["pp"]])
    ratio_tg = round(mtp_tg / ctrl_tg, 4) if (mtp_tg and ctrl_tg) else None
    ratio_pp = round(mtp_pp / ctrl_pp, 4) if (mtp_pp and ctrl_pp) else None
    por_familia = {}
    for f in FAMILIAS:
        c, v = tabla["control"][f]["tg"], tabla["mtp"][f]["tg"]
        r = round(v / c, 4) if (c and v) else None
        por_familia[f] = r
        if r is None:
            fallos.append(f"{f}: falta la medida de algun brazo")
        elif r < UMBRAL_TG_FAMILIA:
            fallos.append(f"{f}: tg {r:.3f}x, por debajo de {UMBRAL_TG_FAMILIA}")
    if ratio_tg is None:
        fallos.append("sin mediana de tg comparable")
    elif ratio_tg < UMBRAL_TG:
        fallos.append(f"tg mediana {ratio_tg:.3f}x, por debajo de {UMBRAL_TG}")
    if ratio_pp is None:
        fallos.append("sin mediana de pp comparable")
    elif ratio_pp < UMBRAL_PP:
        fallos.append(f"pp {ratio_pp:.3f}x, por debajo de {UMBRAL_PP}")

    resumen = {
        "np": 2, "kvu": True, "n_max": n_max, "pasadas": pasadas, "orden": orden,
        "concurrencia": 2, "max_tokens": MAX_TOKENS,
        "cabeza_mtp": os.path.basename(cabeza),
        "corpus": {"objetivo": h034.CORPUS_FAMILIAS, "sha256": corpus["sha256"][:12]},
        "umbrales": {"tg_mediana": UMBRAL_TG, "tg_por_familia": UMBRAL_TG_FAMILIA,
                     "pp": UMBRAL_PP, "greedy": "identico o empate del diagnostico"},
        "tabla": tabla, "ratio_tg": ratio_tg, "ratio_pp": ratio_pp,
        "ratio_tg_por_familia": por_familia,
        "acceptance_mediana": banco.mediana(aceptaciones),
        "especulacion": espec, "greedy": greedy,
        "fallos": fallos, "errores": errores,
    }
    adoptar = not fallos and not errores
    resumen["despliega_mtp"] = adoptar

    def aplicar(texto_unidad: str) -> str:
        if adoptar:
            return banco.pon_mtp(texto_unidad, cabeza, n_max, h034.SPEC_P_MIN)
        return banco.quita_mtp(texto_unidad)

    salida = {"nombre": "h035_np2_kvu_velocidad", "resumen": resumen,
              "adoptar": adoptar, "aplicar": aplicar}
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    ctx.log(f"  H-035 velocidad np=2: adoptar={adoptar} tg x{ratio_tg} pp x{ratio_pp}")
    return salida


# ============================================ 4. vision a np=2 (dos slots)
def np2_kvu_vision(ctx) -> dict:
    """Cuadrado rojo en ambos brazos, y vision + texto a la vez en el MTP."""
    cabeza, n_max = _cabeza(), _nmax()
    clave = banco.clave_de(ctx)
    base = h034._args_productivos(ctx)
    brazos = {"control": list(base), "mtp": args_prod_mtp(base, n_max, cabeza)}
    res, errores = {}, []
    for brazo, args in brazos.items():
        try:
            with _servidor(ctx, args, f"h035-vision-{brazo}",
                           "baseline" if brazo == "control" else "candidato") as srv:
                d = banco.peticion_chat(srv.url, clave, h034.mensajes_vision(),
                                        **_sin_pensar(max_tokens=h034.MAX_TOKENS_VISION,
                                                      cache_prompt=False))
                texto = respuesta_final(d, exigir_stop=True)
                acierta = any(c in texto.lower() for c in h034.COLORES_VISION)
                res[brazo] = {"acierta": acierta, "texto": texto[:40]}
                ctx.medida({"fase": "h035-vision", "brazo": brazo, "config": "solo vision",
                            "timings": timings(d), "esperado": "rojo/red",
                            "obtenido": texto[:40], "veredicto": "ok" if acierta else "falla"})
                if brazo == "mtp":
                    def vis():
                        return banco.peticion_chat(
                            srv.url, clave, h034.mensajes_vision(),
                            **_sin_pensar(max_tokens=h034.MAX_TOKENS_VISION, cache_prompt=False))

                    def txt():
                        return banco.peticion_chat(
                            srv.url, clave,
                            [{"role": "user", "content": PREGUNTA_TEXTO_CONCURRENTE}],
                            **_sin_pensar(max_tokens=16, cache_prompt=False))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        fv, ft = pool.submit(vis), pool.submit(txt)
                        dv, dt = fv.result(), ft.result()
                    tv = respuesta_final(dv, exigir_stop=True)
                    tt = respuesta_final(dt, exigir_stop=True)
                    ok_v = any(c in tv.lower() for c in h034.COLORES_VISION)
                    ok_t = RESPUESTA_TEXTO_CONCURRENTE in tt.upper()
                    res["mtp_concurrente"] = {"vision_acierta": ok_v, "vision": tv[:40],
                                              "texto_acierta": ok_t, "texto": tt[:40]}
                    ctx.medida({"fase": "h035-vision", "brazo": brazo,
                                "config": "vision + texto concurrentes",
                                "esperado": f"rojo/red y {RESPUESTA_TEXTO_CONCURRENTE}",
                                "obtenido": {"vision": tv[:40], "texto": tt[:40]},
                                "veredicto": "ok" if (ok_v and ok_t) else "falla"})
        except (ErrorInfraestructura, FalloContrato) as e:
            errores.append(f"{brazo}: {e}")
    fallos = []
    for b in ("control", "mtp"):
        if b in res and not res[b]["acierta"]:
            fallos.append(f"{b} no dice rojo: {res[b]['texto']!r}")
    c = res.get("mtp_concurrente")
    if c and not (c["vision_acierta"] and c["texto_acierta"]):
        fallos.append(f"con los dos slots ocupados: vision {c['vision']!r}, texto {c['texto']!r}")
    resumen = {"np": 2, "kvu": True, "n_max": n_max, "cabeza_mtp": os.path.basename(cabeza),
               "mmproj": banco.valor_de(base, "mmproj"), "brazos": res,
               "criterio": "control y mtp dicen rojo; y con vision+texto concurrentes "
                           "en el mtp, ambas respuestas correctas",
               "fallos": fallos, "errores": errores}
    adoptar = not fallos and not errores and "mtp_concurrente" in res
    if "mtp_concurrente" not in res and not errores:
        resumen["fallos"].append("no se completo la prueba concurrente")
    salida = {"nombre": "h035_np2_kvu_vision", "resumen": resumen, "adoptar": adoptar}
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    ctx.log(f"  H-035 vision np=2: adoptar={adoptar}")
    return salida
