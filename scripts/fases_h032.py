#!/usr/bin/env python3
"""Fases de H-032: ¿la cache de prompt en RAM devuelve lo que guardo?

Es la deuda directa de H-031, que adopto `--cache-ram 12288` con el beneficio
SIN CUANTIFICAR, apoyandose en que no aparecian lineas de prompt-cache en el
journal. La ausencia de lineas en el journal no prueba inactividad, y una cache
que acelera y contesta con el contexto de otra conversacion no es una
optimizacion: es un fallo de datos servido rapido. Primero correccion, despues
rendimiento, y en ese orden (issue #27148, fix #27624, ambos abiertos).

Las tres fases se cargan por separado con `--fase fases_h032.py:<funcion>` y no
tocan `campana.py`. Baseline y candidato son la MISMA build (df03399): aqui no
se compara codigo, se compara configuracion.

UMBRALES, escritos antes de medir
---------------------------------
`correccion_cache_ram`
    ADOPTAR si y solo si **contaminaciones == 0 en TODAS las configuraciones**
    de `--cache-ram` barridas. Una sola respuesta que traiga el nonce de otra
    conversacion basta para NO adoptar, y el detalle va en `error`. No hay
    umbral estadistico: no se negocia una tasa de fuga de contexto.
    Una configuracion que no se pudo medir (el servidor no arranco) tampoco
    acredita cero contaminaciones, asi que tambien impide adoptar.

`regresion_28495`  (diagnostico, NO vota: no devuelve `adoptar`)
    `regresion_detectada = True` si en alguna configuracion:
      - la mediana de pp de las peticiones 2ª-4ª cae mas de un **20 %**
        respecto a la 1ª (ratio < 0,80), o
      - esa mediana cae mas de un **20 %** respecto a la mediana de `np=1`.
    Se mide desde la 2ª a proposito: la 1ª paga el arranque en frio y
    contamina la comparacion. Se contrasta `np=1` / `np=2 -kvu` / `np=2` sin
    `-kvu` porque el reporte #28495 es sobre HIP/ROCm y aqui somos Vulkan/RADV:
    entra como HIPOTESIS, no como fallo demostrado.

`rendimiento_cache_ram`
    ADOPTAR si el TTFT mediano con `--cache-ram 12288` **no es peor que
    1,05x** el de `--cache-ram 4096`. Es decir, se exige que la cache grande
    no cueste rendimiento; no se le exige que lo gane.

Limitacion conocida (y a la vista, no escondida)
------------------------------------------------
No hay estado compartido entre fases: `Contexto` no lo tiene y no se toca
`campana.py` para anadirlo. Por eso `rendimiento_cache_ram` no puede saber por
si misma si `correccion_cache_ram` encontro contaminacion. El voto combinado lo
hace el runner (todas las fases con `adoptar` deben ser True, y la fase 1 vota
False si hubo fuga), asi que la build/unidad no se promueve; pero el `aplicar`
de la fase 3 seguiria escribiendo `--cache-ram 4096` en vez de desactivarla.
Para el caso en que se quiera **apagar** la cache, se pasa `H032_CONTAMINACION=1`
en el entorno del runner y `aplicar` escribe `--cache-ram 0`. Es un apano
explicito, no un mecanismo: si H-032 sale sucia, lo correcto es ejecutar la
campana con esa variable puesta o dejar el valor a mano.

Parametros por entorno (para acortar en pruebas, nunca para maquillar)
    H032_CACHE_RAM        lista coma-separada  (defecto "0,4096,12288")
    H032_CICLOS           ciclos concurrentes  (defecto 30)
    H032_SEMILLA          semilla del sorteo de parejas (defecto 32032)
    H032_CONTAMINACION    "1" -> `aplicar` de la fase 3 pone --cache-ram 0
    BANCO_PAUSA / BANCO_LIMITE_ARRANQUE / BANCO_GRACIA  (ver banco.py)
"""
from __future__ import annotations

import concurrent.futures
import os
import random
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import banco  # noqa: E402
from validacion import (ErrorInfraestructura, FalloContrato,  # noqa: E402
                        generacion_medida, recuperacion_nonce, timings)

CACHE_RAM_POR_DEFECTO = (0, 4096, 12288)
CICLOS_POR_DEFECTO = 30
CONVERSACIONES = ("A", "B", "C", "D")
CORPUS_SIEMBRA = 2048

# regresion_28495
CORPUS_REGRESION = 16384
PETICIONES_REGRESION = 4          # la 1ª se descarta: paga el arranque en frio
UMBRAL_CAIDA_PP = 0.80            # mediana 2ª-4ª / referencia; < 0,80 = regresion
CONFIGS_28495 = (
    ("np1", {"parallel": 1, "kvu": False}),
    ("np2-kvu", {"parallel": 2, "kvu": True}),
    ("np2", {"parallel": 2, "kvu": False}),
)

# rendimiento_cache_ram
CORPUS_RENDIMIENTO = 8192
CACHE_RAM_RENDIMIENTO = (4096, 12288)
REPETICIONES_TTFT = 5
UMBRAL_TTFT = 1.05                # 12288 no puede ser peor que 1,05x 4096


def lee_unidad_de(ctx) -> str:
    """Texto de la unidad productiva.

    Aislado en una funcion de modulo a proposito: es el unico punto que
    necesita systemd, y asi las pruebas lo sustituyen sin montar una maquina.
    """
    import campana
    return campana.lee_unidad(ctx.unidad)


def _args_productivos(ctx) -> list[str]:
    return banco.args_de_unidad(lee_unidad_de(ctx))


def _servidor(ctx, args, etiqueta, brazo="baseline") -> banco.ServidorBanco:
    return banco.ServidorBanco(
        ctx.binario(brazo), args, ctx.puerto_banco, modelo=ctx.modelo,
        clave=banco.clave_de(ctx), entorno=ctx.entorno_con_clave(),
        log=ctx.log, dir_log=ctx.dir_salida, etiqueta=etiqueta)


def _sin_pensar(**extra) -> dict:
    cuerpo = {"temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    cuerpo.update(extra)
    return cuerpo


# ======================================================== 1. correccion
def _mensajes_siembra(texto_corpus: str, etiqueta: str, nonce: str) -> list[dict]:
    """System + user con ~2k tokens de corpus y el nonce de ESTA conversacion.

    El corpus va delante del codigo para que el prefijo largo sea lo que la
    cache guarda: si la cache cruza contextos, cruza justo por ahi.
    """
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
        {"role": "user",
         "content": "¿Cuál era el CÓDIGO? Responde solo el código."},
    ]


def correccion_cache_ram(ctx) -> dict:
    """Nonces por conversacion, recuperados en PAREJAS CONCURRENTES.

    Cuatro conversaciones A-D, cada una con ~2k tokens de corpus congelado y un
    nonce propio de 12 hex. Despues N ciclos en los que se eligen dos
    conversaciones distintas al azar y se les pregunta su codigo EN PARALELO:
    dos slots tirando a la vez de la misma cache es donde una cache compartida
    puede cruzar contextos, y preguntar de una en una no lo provoca.

    Contrato por respuesta (`validacion.recuperacion_nonce`): el contenido
    normalizado contiene su nonce y NINGUNO de los otros tres -- incluido el de
    la peticion que viaja en paralelo.

    Se mide ademas, por configuracion: TTFT sin cache (1ª peticion a una
    conversacion) contra TTFT con cache (2ª peticion a la misma), `cache_n`
    medio, `prompt_n` real y el RSS del proceso tras llenar la cache.

    No devuelve `aplicar`: esta fase decide si la cache es SEGURA, no cuanta.
    """
    configs = [int(x) for x in os.environ.get(
        "H032_CACHE_RAM", ",".join(str(c) for c in CACHE_RAM_POR_DEFECTO)
    ).split(",") if x.strip()]
    ciclos = int(os.environ.get("H032_CICLOS", CICLOS_POR_DEFECTO))
    semilla = int(os.environ.get("H032_SEMILLA", "32032"))
    corpus = ctx.corpus(CORPUS_SIEMBRA)
    clave = banco.clave_de(ctx)
    base = _args_productivos(ctx)

    resumen = {
        "cache_ram": configs, "ciclos": ciclos, "semilla": semilla,
        "conversaciones": len(CONVERSACIONES),
        "corpus_siembra": {"objetivo": CORPUS_SIEMBRA,
                           "sha256": corpus["sha256"][:12]},
        "umbral": "adoptar solo con contaminaciones == 0 en todas las configs",
        "configs": {},
    }
    problemas: list[str] = []

    for cr in configs:
        args = banco.con_cambios(base, cache_ram=cr, parallel=2, kvu=True)
        etiqueta = f"h032-correccion-cr{cr}"
        r = {"contaminaciones": 0, "fallos_recuperacion": 0, "otros_fallos": 0,
             "peticiones": 0, "ciclos": 0}
        ctx.log(f"  cache-ram {cr}: siembro {len(CONVERSACIONES)} conversaciones "
                f"y lanzo {ciclos} ciclos de dos peticiones concurrentes")
        try:
            with _servidor(ctx, args, etiqueta) as srv:
                r.update(_una_config(ctx, srv, clave, corpus, cr, ciclos,
                                     semilla, problemas))
        except ErrorInfraestructura as e:
            # No haber podido medir NO es haber medido cero contaminaciones.
            r["error"] = str(e)
            problemas.append(f"cache-ram {cr}: {e}")
        resumen["configs"][str(cr)] = r

    contaminaciones = sum(c.get("contaminaciones", 0)
                          for c in resumen["configs"].values())
    resumen["contaminaciones_total"] = contaminaciones
    resumen["configs_sin_medir"] = [k for k, v in resumen["configs"].items()
                                    if v.get("error")]
    adoptar = contaminaciones == 0 and not resumen["configs_sin_medir"]
    salida = {"nombre": "h032_correccion_cache_ram", "resumen": resumen,
              "adoptar": adoptar}
    if not adoptar:
        salida["error"] = ("la cache de prompt no acredita aislamiento entre "
                           "conversaciones: " + "; ".join(problemas[:6]))
    return salida


def _una_config(ctx, srv, clave, corpus, cr, ciclos, semilla, problemas) -> dict:
    """Siembra + ciclos concurrentes contra un servidor de banco ya en pie."""
    rng = random.Random(semilla + cr)
    nonces = {et: secrets.token_hex(6) for et in CONVERSACIONES}
    siembras = {et: _mensajes_siembra(corpus["texto"], et, nonces[et])
                for et in CONVERSACIONES}
    ttft_sin, ttft_con, cache_ns = [], [], []
    prompt_n_real = None

    for et in CONVERSACIONES:
        for vuelta, marca in ((1, "sin_cache"), (2, "con_cache")):
            d = banco.peticion_chat(
                srv.url, clave, siembras[et],
                **_sin_pensar(max_tokens=16, cache_prompt=True))
            t = timings(d)
            # Con cache_prompt=true, `prompt_n` cuenta SOLO los tokens que el
            # servidor tuvo que procesar: en la 2a vuelta sale ~4 (el resto
            # vino de cache). El tamano real del prompt se verifica con la
            # 1a vuelta, que es fria. Visto en el M5 el 11-09: la fase abortaba
            # con "el corpus de 2048 mide 4".
            if vuelta == 1:
                prompt_n_real = int(t["prompt_n"])
            (ttft_sin if vuelta == 1 else ttft_con).append(banco.ttft_ms(d))
            ctx.medida({
                "fase": "h032-correccion", "brazo": "baseline",
                "config": f"cache-ram {cr}", "cache_ram": cr,
                "etapa": "siembra", "conversacion": et, "vuelta": vuelta,
                "marca": marca, "prompt_n": int(t["prompt_n"]),
                "prompt_n_corpus": prompt_n_real,
                "prompt_ms": banco.ttft_ms(d), "cache_n": banco.cache_n(d),
                "timings": t, "esperado": "OK",
                "obtenido": _texto(d)[:80],
            })
    ctx.verifica_corpus(CORPUS_SIEMBRA, prompt_n_real)
    rss = srv.rss_mb()

    contaminaciones = fallos = otros = peticiones = 0
    for ciclo in range(1, ciclos + 1):
        par = rng.sample(CONVERSACIONES, 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futuros = {pool.submit(_pregunta_una, srv, clave, siembras[et]): et
                       for et in par}
            hechos = {futuros[f]: f for f in concurrent.futures.as_completed(futuros)}
        for et in par:
            peticiones += 1
            ajenos = [n for e2, n in nonces.items() if e2 != et]
            reg = {"fase": "h032-correccion", "brazo": "baseline",
                   "config": f"cache-ram {cr}", "cache_ram": cr,
                   "etapa": "ciclo", "ciclo": ciclo, "conversacion": et,
                   "concurrente_con": [e for e in par if e != et],
                   "esperado": nonces[et]}
            try:
                d = hechos[et].result()
            except ErrorInfraestructura as e:
                otros += 1
                reg.update(veredicto="infraestructura", obtenido=None, error=str(e))
                problemas.append(f"cache-ram {cr} ciclo {ciclo} {et}: {e}")
                ctx.medida(reg)
                continue
            t = timings(d)
            reg.update(prompt_n=int(t["prompt_n"]), prompt_ms=banco.ttft_ms(d),
                       cache_n=banco.cache_n(d), timings=t,
                       obtenido=_texto(d)[:120])
            cache_ns.append(banco.cache_n(d))
            try:
                recuperacion_nonce(d, nonces[et], ajenos)
                reg["veredicto"] = "ok"
            except FalloContrato as e:
                motivo = getattr(e, "motivo", "contrato")
                reg.update(veredicto=motivo, error=str(e))
                if motivo == "contaminacion":
                    contaminaciones += 1
                elif motivo == "no_recupera":
                    fallos += 1
                else:
                    otros += 1
                problemas.append(f"cache-ram {cr} ciclo {ciclo} {et}: {e}")
            ctx.medida(reg)

    return {
        "contaminaciones": contaminaciones, "fallos_recuperacion": fallos,
        "otros_fallos": otros, "peticiones": peticiones, "ciclos": ciclos,
        "cache_n_medio": round(sum(cache_ns) / len(cache_ns), 1) if cache_ns else None,
        "ttft_sin_cache_ms": banco.mediana(ttft_sin),
        "ttft_con_cache_ms": banco.mediana(ttft_con),
        "rss_mb": rss, "prompt_n": prompt_n_real,
    }


def _pregunta_una(srv, clave, siembra):
    return banco.peticion_chat(
        srv.url, clave, _mensajes_pregunta(siembra),
        **_sin_pensar(max_tokens=32, cache_prompt=True))


def _texto(d: dict) -> str:
    try:
        return (d["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


# ======================================================== 2. regresion #28495
def regresion_28495(ctx) -> dict:
    """Contraste `np=1` / `np=2 -kvu` / `np=2` sin `-kvu` sobre peticiones largas.

    Entra como HIPOTESIS: el reporte #28495 es sobre HIP/ROCm y aqui la pila es
    Vulkan/RADV, asi que puede no aplicarnos en absoluto. Por eso esta fase es
    DIAGNOSTICO y no vota: no devuelve `adoptar` ni `aplicar`. Su trabajo es
    dejar los numeros escritos, no decidir nada.

    Cuatro peticiones consecutivas por configuracion con el corpus de 16.384
    tokens, `cache_prompt=false` (metodologia, regla 9: con reutilizacion de
    prefijo el pp no mide computo). La 1ª SE DESCARTA -- paga el arranque en
    frio -- y se compara la mediana de la 2ª-4ª contra ella y contra `np=1`.

    Umbral: caida > 20 % (ratio < 0,80) en cualquiera de las dos comparaciones
    marca `regresion_detectada = True`.
    """
    corpus = ctx.corpus(CORPUS_REGRESION)
    clave = banco.clave_de(ctx)
    base = _args_productivos(ctx)
    mensajes = [{"role": "user", "content": corpus["texto"]}]

    resumen = {
        "corpus": {"objetivo": CORPUS_REGRESION, "sha256": corpus["sha256"][:12]},
        "peticiones_por_config": PETICIONES_REGRESION,
        "umbral_caida": round(1 - UMBRAL_CAIDA_PP, 2),
        "nota": "hipotesis: #28495 se reporta sobre HIP/ROCm y aqui es Vulkan/RADV",
        "configs": {},
    }
    motivos: list[str] = []      # caidas de pp: lo que marca la regresion
    errores: list[str] = []      # instrumental caido: no es una regresion

    for nombre, cambios in CONFIGS_28495:
        args = banco.con_cambios(base, **cambios)
        r = {"cambios": {k: v for k, v in cambios.items()}}
        try:
            with _servidor(ctx, args, f"h032-28495-{nombre}") as srv:
                pps, tgs, pn = [], [], None
                for i in range(1, PETICIONES_REGRESION + 1):
                    d = banco.peticion_chat(
                        srv.url, clave, mensajes,
                        **_sin_pensar(max_tokens=64, cache_prompt=False))
                    m = generacion_medida(d, 64)
                    pn = int(m["prompt_n"])
                    pps.append(m["pp"])
                    tgs.append(m["tg"])
                    ctx.medida({
                        "fase": "h032-28495", "brazo": "baseline",
                        "config": nombre, "peticion": i,
                        "descartada": i == 1, "prompt_n": pn,
                        "prompt_ms": banco.ttft_ms(d), "cache_n": banco.cache_n(d),
                        "timings": timings(d), "pp": m["pp"], "tg": m["tg"],
                        "esperado": f"pp estable en {nombre}",
                        "obtenido": round(m["pp"], 2),
                    })
                ctx.verifica_corpus(CORPUS_REGRESION, pn)
            r.update(prompt_n=pn, pp_primera=pps[0], pp_resto=pps[1:],
                     mediana_pp_2_4=banco.mediana(pps[1:]),
                     mediana_tg_2_4=banco.mediana(tgs[1:]))
            r["ratio_2mas_vs_1"] = (round(r["mediana_pp_2_4"] / pps[0], 4)
                                    if pps[0] else None)
        except ErrorInfraestructura as e:
            r["error"] = str(e)
            errores.append(f"{nombre}: {e}")
        resumen["configs"][nombre] = r

    ref = resumen["configs"].get("np1", {}).get("mediana_pp_2_4")
    for nombre, r in resumen["configs"].items():
        if r.get("error"):
            continue
        if r.get("ratio_2mas_vs_1") is not None and r["ratio_2mas_vs_1"] < UMBRAL_CAIDA_PP:
            motivos.append(f"{nombre}: la mediana de la 2ª-4ª cae al "
                           f"{r['ratio_2mas_vs_1']:.0%} de la 1ª")
        if ref and r.get("mediana_pp_2_4"):
            r["ratio_vs_np1"] = round(r["mediana_pp_2_4"] / ref, 4)
            if nombre != "np1" and r["ratio_vs_np1"] < UMBRAL_CAIDA_PP:
                motivos.append(f"{nombre}: la mediana cae al "
                               f"{r['ratio_vs_np1']:.0%} de la de np=1")

    # Una configuracion que no se pudo medir NO es una regresion de
    # rendimiento: es instrumental caido, y se cuenta aparte (metodologia,
    # regla 6). Va en `error`, no en `regresion_detectada`.
    resumen["motivos"] = motivos
    resumen["errores"] = errores
    resumen["regresion_detectada"] = bool(motivos)
    salida = {"nombre": "h032_regresion_28495", "resumen": resumen}
    if errores:
        salida["error"] = ("no pude medir " + ", ".join(
            k for k, v in resumen["configs"].items() if v.get("error"))
            + ": " + "; ".join(errores[:3]))
    return salida


# ======================================================== 3. rendimiento
def rendimiento_cache_ram(ctx) -> dict:
    """¿Cuanto TTFT compra `--cache-ram 12288` frente a 4096?

    Con la configuracion productiva (sin tocar `np` ni `-kvu`), un contexto de
    8.192 tokens: una peticion de cebado y despues 5 repeticiones identicas con
    `cache_prompt=true`. Cada una de esas 5 es una "2ª peticion" contra una
    cache ya caliente; se reporta la MEDIANA de su TTFT y el `cache_n`.

    Umbral: adoptar si el TTFT mediano de 12288 no supera 1,05x el de 4096. Se
    exige que la cache grande no cueste; no se le exige ganar.

    `aplicar` deja `--cache-ram 12288` si adopta y lo baja a 4096 si no. Con
    `H032_CONTAMINACION=1` en el entorno escribe 0: ver la limitacion
    documentada en la cabecera del modulo (no hay estado entre fases).
    """
    corpus = ctx.corpus(CORPUS_RENDIMIENTO)
    clave = banco.clave_de(ctx)
    base = _args_productivos(ctx)
    mensajes = [{"role": "user", "content": corpus["texto"]}]

    resumen = {
        "corpus": {"objetivo": CORPUS_RENDIMIENTO, "sha256": corpus["sha256"][:12]},
        "repeticiones": REPETICIONES_TTFT, "umbral_ttft": UMBRAL_TTFT,
        "criterio": f"adoptar si ttft(12288) <= {UMBRAL_TTFT}x ttft(4096)",
        "configs": {},
    }
    errores: list[str] = []

    for cr in CACHE_RAM_RENDIMIENTO:
        args = banco.con_cambios(base, cache_ram=cr)
        r = {}
        try:
            with _servidor(ctx, args, f"h032-rendimiento-cr{cr}") as srv:
                ttfts, cns, pn = [], [], None
                for i in range(0, REPETICIONES_TTFT + 1):
                    d = banco.peticion_chat(
                        srv.url, clave, mensajes,
                        **_sin_pensar(max_tokens=32, cache_prompt=True))
                    t = timings(d)
                    if i == 0:
                        # el cebado es la unica peticion fria: con cache
                        # caliente prompt_n solo cuenta lo recomputado.
                        pn = int(t["prompt_n"])
                    if i:                       # i == 0 es el cebado
                        ttfts.append(banco.ttft_ms(d))
                        cns.append(banco.cache_n(d))
                    ctx.medida({
                        "fase": "h032-rendimiento", "brazo": "baseline",
                        "config": f"cache-ram {cr}", "cache_ram": cr,
                        "repeticion": i, "cebado": i == 0,
                        "prompt_n": int(t["prompt_n"]), "prompt_n_corpus": pn,
                        "prompt_ms": banco.ttft_ms(d), "cache_n": banco.cache_n(d),
                        "timings": t, "esperado": "ttft con cache caliente",
                        "obtenido": banco.ttft_ms(d),
                    })
                ctx.verifica_corpus(CORPUS_RENDIMIENTO, pn)
                r.update(prompt_n=pn, ttft_ms=ttfts,
                         ttft_mediano_ms=banco.mediana(ttfts),
                         cache_n_medio=round(sum(cns) / len(cns), 1) if cns else None,
                         rss_mb=srv.rss_mb())
        except ErrorInfraestructura as e:
            r["error"] = str(e)
            errores.append(f"cache-ram {cr}: {e}")
        resumen["configs"][str(cr)] = r

    t4 = resumen["configs"].get("4096", {}).get("ttft_mediano_ms")
    t12 = resumen["configs"].get("12288", {}).get("ttft_mediano_ms")
    resumen["ratio_12288_sobre_4096"] = round(t12 / t4, 4) if (t4 and t12) else None
    adoptar = bool(t4 and t12) and t12 <= UMBRAL_TTFT * t4
    resumen["adoptar"] = adoptar
    contaminada = os.environ.get("H032_CONTAMINACION") == "1"
    resumen["cache_ram_a_aplicar"] = 0 if contaminada else (12288 if adoptar else 4096)
    resumen["contaminacion_declarada"] = contaminada

    def aplicar(texto_unidad: str) -> str:
        return banco.pon_cache_ram(texto_unidad, resumen["cache_ram_a_aplicar"])

    salida = {"nombre": "h032_rendimiento_cache_ram", "resumen": resumen,
              "adoptar": adoptar, "aplicar": aplicar}
    if errores:
        salida["error"] = "; ".join(errores)
    return salida
