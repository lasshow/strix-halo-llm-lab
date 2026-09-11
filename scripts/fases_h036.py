#!/usr/bin/env python3
"""H-036: los cambios de `drluoto/llama.cpp` (rama strix-halo-vulkan), uno a uno.

Referencia externa (H-031b): mismo Bosgame M5, mismo punto de partida (27 t/s
sin especulacion), reporta 33-63 t/s con una pila entera cambiada a la vez.
Aqui NO se adopta el arbol: se evalua **un cambio por brazo** contra la
produccion vigente (H-035c: df03399 + PR28501 + PR28243, cabeza MTP Unsloth
Q8_0, n-max 2, np=2 -kvu, mmproj). Cada fase es un A/B independiente con el
nucleo de H-035 (`fases_h035.ab_np2`: np=2, dos peticiones concurrentes por
familia, greedy juzgado por logprobs del control).

Lo que YA tenemos de su pila y no se vuelve a medir: row-id hoisting (PR
#28501, H-033) y "siempre los N tokens de borrador" (`--spec-draft-p-min 0`,
H-034). `GGML_VK_DISABLE_GDN_CACHE_FUSION` no aplica: esa fusion es un port
suyo de #27973 que nuestro upstream no lleva. `--ctx-checkpoints` ya vale 32
por defecto.

Brazos, en orden de coste (se corren en este orden; cada uno decide solo):

  B. `nmax3`      misma build, `--spec-draft-n-max 3` (drluoto: su default).
                  Solo flag. H-034 a np=1 dio n-max 3 > 2 en codigo/json y
                  n-max 3 < 2 en creativo; aqui se decide a np=2.
  A. `frspec`     cabeza `mtp-...-Q8_0-frspec-65k.gguf` (drluoto, HF) con los
                  6 parches qwen4exp de FR-Spec portados. Vocabulario del
                  borrador recortado a 65k -> borrador mas barato -> la promesa
                  es prosa/creativo, donde MTP casi no ayuda (x1,1).
  C. `requant`    tronco requantizado: densos Q5_K + routers Q8_0 (parche
                  LLAMA_QUANT_ALLOW_ROUTER). Otro GGUF de ~90 GB.
  D. `kvzero`     KV cells a cero al liberar (nathanw1014). Determinismo, no
                  velocidad: umbral tg >= 0,98x (no perder), greedy limpio.

Umbrales (fijados antes de medir): B/A/C adoptan con tg mediana >= 1,08x,
ninguna familia < 0,95x, pp >= 0,97x, cero `no_verificado`. D adopta con
tg >= 0,98x y pp >= 0,97x. Un brazo que adopta se despliega en su propia
campana (campana.py aplica + promueve + gate); los brazos son acumulativos:
cada uno se mide contra lo que quedo desplegado del anterior.

Variables de entorno:
  H036_FRSPEC_HEAD   ruta de la cabeza FR-Spec (defecto en /models/gguf/.../MTP/)
  H036_REQUANT_GGUF  ruta del tronco requantizado (primer shard)
  H036_NMAX          n-max del brazo B (defecto 3)
"""
from __future__ import annotations

import os
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

import banco  # noqa: E402
import fases_h034 as h034  # noqa: E402
import fases_h035 as h035  # noqa: E402

# Cabeza FR-Spec construida AQUI (scripts/construir-cabeza-frspec.py) a partir de
# la cabeza Unsloth de produccion + output.weight recortado + d2t de drluoto. La
# cabeza publicada por drluoto es otra conversion (fc_embd/fc_hidden, hc_*) y
# nuestra build no la carga: usarla habria cambiado dos cosas a la vez.
FRSPEC_HEAD = ("/models/gguf/qwen38-flash-next/MTP/"
               "mtp-Qwen3.8-Flash-Next-shared-Q8_0-frspec65k-unsloth.gguf")
REQUANT_GGUF = ("/models/gguf/qwen38-flash-next/Q5K-router-Q8/"
                "Qwen3.8-Flash-Next-Q5K-routerQ8-00001-of-00003.gguf")

UMBRAL_TG = 1.08
UMBRAL_TG_FAMILIA = 0.95
UMBRAL_PP = 0.97
UMBRAL_TG_NO_PERDER = 0.98


def _prod(ctx) -> list[str]:
    """La linea productiva TAL CUAL (con su MTP): es el control de todo H-036."""
    args = h034._args_productivos(ctx)
    if banco.valor_de(args, "spec_type") != "draft-mtp":
        raise RuntimeError(
            "H-036 exige que produccion lleve ya la cabeza MTP (H-035c): la "
            "unidad no trae --spec-type draft-mtp, asi que el control no es el previsto")
    return args


def _referencia(prod: list[str]) -> list[str]:
    """La linea productiva SIN especulacion: la unica que devuelve logprobs de
    todos los tokens y por tanto la unica referencia valida para juzgar greedy
    cuando control y candidato llevan MTP (ver `fases_h035.ab_np2`)."""
    return banco.quita_flags(prod, ("--spec-type", "-md", "--spec-draft-n-max",
                                    "--spec-draft-p-min"))


def _veredicto(ctx, nombre, resumen, aplicar=None, exigir=None) -> dict:
    fallos, errores = resumen["fallos"], resumen["errores"]
    adoptar = not fallos and not errores
    salida = {"nombre": nombre, "resumen": resumen, "adoptar": adoptar}
    if aplicar is not None:
        salida["aplicar"] = aplicar
    if fallos or errores:
        salida["error"] = "; ".join(fallos + errores)
    ctx.log(f"  H-036 {nombre}: adoptar={adoptar} tg x{resumen['ratio_tg']} "
            f"pp x{resumen['ratio_pp']} fam={resumen['ratio_tg_por_familia']}")
    return salida


# ------------------------------------------------------------------ B. n-max 3
def nmax3(ctx) -> dict:
    """Produccion (n-max 2) contra la misma build con n-max 3. Misma build en
    los dos brazos: `build_de` devuelve 'baseline' para ambos."""
    n = int(os.environ.get("H036_NMAX", 3))
    prod = _prod(ctx)
    cand = banco.con_cambios(prod, spec_draft_n_max=n)
    resumen = h035.ab_np2(ctx, {"control": prod, f"nmax{n}": cand},
                          "h036-nmax", "h036-nmax",
                          referencia=_referencia(prod),
                          umbral_tg=UMBRAL_TG, umbral_tg_familia=UMBRAL_TG_FAMILIA,
                          umbral_pp=UMBRAL_PP, build_de=lambda b: "baseline")
    resumen["n_max_control"] = int(banco.valor_de(prod, "spec_draft_n_max"))
    resumen["n_max_candidato"] = n
    adoptar = not resumen["fallos"] and not resumen["errores"]

    def aplicar(texto_unidad: str) -> str:
        if not adoptar:
            return texto_unidad
        cabeza = banco.valor_de(prod, "draft_model")
        return banco.pon_mtp(texto_unidad, cabeza, n, float(banco.valor_de(prod, "spec_draft_p_min") or 0))

    return _veredicto(ctx, "h036_nmax3", resumen, aplicar)


# ------------------------------------------------------------ A. cabeza FR-Spec
def frspec(ctx) -> dict:
    """Produccion (cabeza Unsloth Q8_0) contra la build con los parches FR-Spec
    y la cabeza recortada a 65k. El candidato necesita la build 'candidato'."""
    cabeza = os.environ.get("H036_FRSPEC_HEAD", FRSPEC_HEAD)
    if not os.path.exists(cabeza):
        return {"nombre": "h036_frspec", "adoptar": False,
                "error": f"no existe la cabeza FR-Spec {cabeza}",
                "resumen": {"cabeza": cabeza}}
    prod = _prod(ctx)
    cand = banco.con_cambios(prod, draft_model=cabeza)
    resumen = h035.ab_np2(ctx, {"control": prod, "frspec": cand},
                          "h036-frspec", "h036-frspec",
                          referencia=_referencia(prod),
                          umbral_tg=UMBRAL_TG, umbral_tg_familia=UMBRAL_TG_FAMILIA,
                          umbral_pp=UMBRAL_PP)
    resumen["cabeza_control"] = os.path.basename(banco.valor_de(prod, "draft_model"))
    resumen["cabeza_candidato"] = os.path.basename(cabeza)
    adoptar = not resumen["fallos"] and not resumen["errores"]

    def aplicar(texto_unidad: str) -> str:
        if not adoptar:
            return texto_unidad
        return banco.pon_mtp(texto_unidad, cabeza,
                             int(banco.valor_de(prod, "spec_draft_n_max")),
                             float(banco.valor_de(prod, "spec_draft_p_min") or 0))

    return _veredicto(ctx, "h036_frspec", resumen, aplicar)


# --------------------------------------------------------------- C. requant
def requant(ctx) -> dict:
    """Produccion (UD-IQ4_XS) contra el tronco requantizado. Misma build."""
    gguf = os.environ.get("H036_REQUANT_GGUF", REQUANT_GGUF)
    if not os.path.exists(gguf):
        return {"nombre": "h036_requant", "adoptar": False,
                "error": f"no existe el tronco requantizado {gguf}",
                "resumen": {"gguf": gguf}}
    prod = _prod(ctx)
    cand = banco.con_cambios(prod, model=gguf)
    resumen = h035.ab_np2(ctx, {"control": prod, "requant": cand},
                          "h036-requant", "h036-requant",
                          referencia=_referencia(prod),
                          umbral_tg=UMBRAL_TG, umbral_tg_familia=UMBRAL_TG_FAMILIA,
                          umbral_pp=UMBRAL_PP, build_de=lambda b: "baseline")
    resumen["modelo_control"] = os.path.basename(banco.valor_de(prod, "model"))
    resumen["modelo_candidato"] = os.path.basename(gguf)
    adoptar = not resumen["fallos"] and not resumen["errores"]

    def aplicar(texto_unidad: str) -> str:
        if not adoptar:
            return texto_unidad
        return banco.pon_modelo(texto_unidad, gguf)

    return _veredicto(ctx, "h036_requant", resumen, aplicar)


# --------------------------------------------------------------- D. kv zero
def kvzero(ctx) -> dict:
    """Produccion contra la build con el parche de KV a cero. Solo la build
    cambia (no hay flag): control=baseline, kvzero=candidato. Criterio: no
    perder velocidad y greedy limpio."""
    prod = _prod(ctx)
    resumen = h035.ab_np2(ctx, {"control": prod, "kvzero": list(prod)},
                          "h036-kvzero", "h036-kvzero",
                          referencia=_referencia(prod),
                          umbral_tg=UMBRAL_TG_NO_PERDER, umbral_tg_familia=UMBRAL_TG_FAMILIA,
                          umbral_pp=UMBRAL_PP)
    return _veredicto(ctx, "h036_kvzero", resumen)
