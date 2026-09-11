#!/usr/bin/env python3
"""Fase de H-033: df03399 contra df03399 + PR #28501, y nada mas.

A/B minimo. El candidato es la baseline MAS el parche de esa PR, fijado por SHA
exacto: una PR es una rama movil y "la PR #28501" sin SHA no es una
configuracion reproducible. Cualquier otra diferencia entre brazos invalida el
punto, asi que los dos brazos arrancan con la MISMA linea de argumentos, la de
la unidad productiva, y con el MISMO corpus congelado.

Por que alternado A,B,A,B y no "todo A, luego todo B": la maquina no es estable
a lo largo de una hora (temperatura, paginas frias, lo que haya dejado el brazo
anterior). Medir A entero y despues B entero regala al segundo brazo cualquier
deriva termica que haya ocurrido; alternar la reparte entre los dos.

UMBRALES, escritos antes de medir
---------------------------------
ADOPTAR si y solo si se cumplen las tres cosas:

  1. `pp` candidato >= **1,05x** el de la baseline **en los dos tamanos**
     (8.192 y 32.768 tokens). Una mejora que solo aparece en un tamano no es
     la mejora que promete la PR.
  2. `tg` candidato >= **0,98x** el de la baseline en los dos tamanos: se
     admite un 2 % de ruido, no un peaje.
  3. **Igualdad greedy**: 3 prompts cortos con `temperature 0`, `seed 42` y
     `max_tokens 64` tienen que devolver un `content` IDENTICO en los dos
     brazos. La PR promete salida greedy identica; si el texto cambia, no es
     una build mas rapida, es otra build, y el A/B de velocidad ya no compara
     lo mismo. Si difiere: `adoptar=False` y el detalle va en `error`.

Se comparan MEDIANAS de 2 rondas por tamano y brazo.

No devuelve `aplicar`: aqui no hay nada que escribir en la unidad. La promocion
de la build la hace el runner (`builds.sh promover`) cuando el voto sale True y
los gates dan verde.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import banco  # noqa: E402
from validacion import ErrorInfraestructura, generacion_medida, timings  # noqa: E402

BRAZOS = ("baseline", "candidato")
TAMANOS = (8192, 32768)
RONDAS = 2
MAX_TOKENS = 160
UMBRAL_PP = 1.05        # candidato >= 1,05x baseline, en AMBOS tamanos
UMBRAL_TG = 0.98        # y sin peaje de generacion mas alla del 2 %

# Cortos, deterministas y de respuesta corta: lo que se compara es el texto
# exacto entre brazos, no si el modelo acierta.
PROMPTS_GREEDY = (
    "¿Cuánto es 17 por 23? Responde solo el número.",
    "Escribe los tres primeros números primos separados por comas.",
    "Traduce al inglés, sin comillas: el horno está frío.",
)
SEMILLA_GREEDY = 42


def lee_unidad_de(ctx) -> str:
    """Texto de la unidad productiva. Aislado para poder sustituirlo en pruebas."""
    import campana
    return campana.lee_unidad(ctx.unidad)


def _servidor(ctx, args, brazo, ronda):
    return banco.ServidorBanco(
        ctx.binario(brazo), args, ctx.puerto_banco, modelo=ctx.modelo,
        clave=banco.clave_de(ctx), entorno=ctx.entorno_con_clave(),
        log=ctx.log, dir_log=ctx.dir_salida,
        etiqueta=f"h033-{brazo}-r{ronda}")


def _sin_pensar(**extra) -> dict:
    cuerpo = {"temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    cuerpo.update(extra)
    return cuerpo


def ab_builds(ctx) -> dict:
    """A/B alternado entre las dos builds, con comprobacion de igualdad greedy.

    Orden real de ejecucion: A, B, A, B. En cada visita a un brazo se levanta
    su servidor de banco con la linea productiva, se miden los dos tamanos con
    `cache_prompt=false` (regla 9 de la metodologia) y, en la primera ronda, se
    recogen las tres salidas greedy.
    """
    clave = banco.clave_de(ctx)
    args = banco.args_de_unidad(lee_unidad_de(ctx))
    corpus = {t: ctx.corpus(t) for t in TAMANOS}

    medidas = {b: {t: {"pp": [], "tg": []} for t in TAMANOS} for b in BRAZOS}
    greedy: dict[str, list[str]] = {}
    orden: list[str] = []
    errores: list[str] = []

    for ronda in range(1, RONDAS + 1):
        for brazo in BRAZOS:
            orden.append(brazo)
            try:
                with _servidor(ctx, args, brazo, ronda) as srv:
                    for t in TAMANOS:
                        d = banco.peticion_chat(
                            srv.url, clave,
                            [{"role": "user", "content": corpus[t]["texto"]}],
                            **_sin_pensar(max_tokens=MAX_TOKENS, cache_prompt=False))
                        m = generacion_medida(d, MAX_TOKENS)
                        ctx.verifica_corpus(t, int(m["prompt_n"]))
                        medidas[brazo][t]["pp"].append(m["pp"])
                        medidas[brazo][t]["tg"].append(m["tg"])
                        ctx.medida({
                            "fase": "h033-ab-builds", "brazo": brazo,
                            "config": f"corpus {t}", "ronda": ronda,
                            "tamano": t, "prompt_n": int(m["prompt_n"]),
                            "prompt_ms": banco.ttft_ms(d),
                            "cache_n": banco.cache_n(d), "timings": timings(d),
                            "pp": m["pp"], "tg": m["tg"],
                            "predicted_n": m["predicted_n"],
                            "esperado": f"pp/tg de {brazo} a {t} tokens",
                            "obtenido": {"pp": round(m["pp"], 2),
                                         "tg": round(m["tg"], 2)},
                        })
                    if ronda == 1:
                        greedy[brazo] = _greedy(ctx, srv, clave, brazo)
            except ErrorInfraestructura as e:
                errores.append(f"{brazo} ronda {ronda}: {e}")

    resumen = {
        "orden": orden, "rondas": RONDAS, "tamanos": list(TAMANOS),
        "max_tokens": MAX_TOKENS,
        "umbrales": {"pp": UMBRAL_PP, "tg": UMBRAL_TG,
                     "greedy": "content identico entre brazos"},
        "corpus": {str(t): corpus[t]["sha256"][:12] for t in TAMANOS},
        "medianas": {}, "ratios": {},
    }
    for brazo in BRAZOS:
        resumen["medianas"][brazo] = {
            str(t): {"pp": banco.mediana(medidas[brazo][t]["pp"]),
                     "tg": banco.mediana(medidas[brazo][t]["tg"]),
                     "muestras": len(medidas[brazo][t]["pp"])}
            for t in TAMANOS}

    fallos: list[str] = []
    for t in TAMANOS:
        b = resumen["medianas"]["baseline"][str(t)]
        c = resumen["medianas"]["candidato"][str(t)]
        par = {}
        for met, umbral, sentido in (("pp", UMBRAL_PP, ">="), ("tg", UMBRAL_TG, ">=")):
            if not b[met] or not c[met]:
                par[met] = None
                fallos.append(f"{met} a {t}: falta la medida de algun brazo")
                continue
            par[met] = round(c[met] / b[met], 4)
            if par[met] < umbral:
                fallos.append(f"{met} a {t} tokens: {par[met]:.3f}x, por debajo "
                              f"del umbral {sentido} {umbral}")
        resumen["ratios"][str(t)] = par

    identico = None
    if len(greedy) == len(BRAZOS):
        distintos = [i for i, (a, b) in
                     enumerate(zip(greedy["baseline"], greedy["candidato"]))
                     if a != b]
        identico = not distintos
        resumen["greedy"] = {
            "prompts": len(PROMPTS_GREEDY), "seed": SEMILLA_GREEDY,
            "identico": identico, "indices_distintos": distintos,
        }
        if distintos:
            i = distintos[0]
            fallos.append(
                f"la salida greedy difiere en {len(distintos)}/{len(PROMPTS_GREEDY)} "
                f"prompts (el nº {i + 1}: baseline {greedy['baseline'][i][:80]!r} "
                f"vs candidato {greedy['candidato'][i][:80]!r}); la PR promete "
                "salida greedy identica, asi que no es la misma build midiendo "
                "mas rapido")
    else:
        resumen["greedy"] = {"identico": None,
                             "motivo": "no se pudo medir algun brazo"}
        fallos.append("no hay comparacion greedy: falto un brazo")

    resumen["fallos"] = fallos
    resumen["errores"] = errores
    adoptar = not fallos and not errores
    salida = {"nombre": "h033_ab_builds", "resumen": resumen, "adoptar": adoptar}
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    return salida


def _greedy(ctx, srv, clave, brazo) -> list[str]:
    """Las tres salidas greedy de este brazo, en orden."""
    textos = []
    for i, prompt in enumerate(PROMPTS_GREEDY, 1):
        d = banco.peticion_chat(
            srv.url, clave, [{"role": "user", "content": prompt}],
            **_sin_pensar(max_tokens=64, seed=SEMILLA_GREEDY, cache_prompt=False))
        # Contrato de microbenchmark a proposito: lo que se compara es el TEXTO
        # entre brazos, no si el modelo acierta. Exigir `finish_reason=stop`
        # aqui convertiria una respuesta larga en un fallo de la PR.
        texto = generacion_medida(d, 64)["texto"]
        textos.append(texto)
        ctx.medida({
            "fase": "h033-greedy", "brazo": brazo, "config": "greedy",
            "prompt_i": i, "seed": SEMILLA_GREEDY,
            "prompt_n": int(timings(d)["prompt_n"]), "timings": timings(d),
            "esperado": "identico al otro brazo", "obtenido": texto[:200],
        })
    return textos
