#!/usr/bin/env python3
"""Contratos de validacion de respuestas del servidor. Un contrato por tipo de prueba.

Por que existe este modulo (H-024, auditoria externa del corte 35a2f26):

  1. `mide()` en bench-ubatch.py hacia `d.get("timings", {})` con valores por
     defecto cero. Un HTTP 200 con cuerpo `{}` devolvia (0, 0, 0) sin excepcion
     y la fila se etiquetaba `ok`. Una medicion ausente no es una medicion de cero.

  2. La correccion no puede ser "exigir siempre content y finish_reason=stop".
     El servidor distingue finalizacion normal, agotamiento del presupuesto de
     generacion y llamada a herramientas: son tres cosas con significados
     distintos. Un microbenchmark que genera 160 tokens y para por presupuesto
     es una medida VALIDA; una tarea que debe entregar una respuesta completa y
     para por presupuesto es un FALLO. El contrato depende de la prueba.

  3. Hay que separar "el modelo falla la prueba" de "el banco no ha podido
     ejecutarla". Un timeout, un 503 o un JSON roto no son un modelo malo: son
     instrumental caido, y contaminan la tasa de acierto si se mezclan.

Las dos excepciones son la frontera:
    ErrorInfraestructura -> no cuenta como fallo del modelo, invalida la medida.
    FalloContrato        -> el modelo respondio y su respuesta no cumple lo pedido.
"""
from __future__ import annotations

import json
import math

# Techo de plausibilidad para una tasa (t/s). No es una medida del hardware:
# es una barrera contra metricas corruptas y contra prefill reaprovechado de
# cache, que produce cifras de pp absurdas que no miden computo alguno.
LIMITE_TASA = 1_000_000.0


class ErrorInfraestructura(Exception):
    """El banco de pruebas no pudo obtener una respuesta evaluable.

    HTTP != 200, timeout, conexion rechazada, cuerpo no parseable, o respuesta
    sin la estructura minima de la API. Nunca debe contabilizarse como error
    del modelo ni convertirse en una medida de cero.
    """


class FalloContrato(Exception):
    """El modelo respondio, pero la respuesta no cumple el contrato de la prueba."""


def cuerpo_json(bruto: bytes | str) -> dict:
    """Parsea el cuerpo HTTP. Un cuerpo no parseable es infraestructura, no modelo."""
    try:
        d = json.loads(bruto)
    except Exception as e:
        muestra = (bruto if isinstance(bruto, str) else bruto.decode("utf-8", "replace"))[:200]
        raise ErrorInfraestructura(f"cuerpo no parseable: {e}; empieza por {muestra!r}") from e
    if not isinstance(d, dict):
        raise ErrorInfraestructura(f"cuerpo JSON no es un objeto sino {type(d).__name__}")
    if "error" in d:
        raise ErrorInfraestructura(f"el servidor devolvio error: {str(d['error'])[:200]}")
    return d


def _eleccion(d: dict) -> tuple[dict, str]:
    ch = d.get("choices")
    if not isinstance(ch, list) or not ch:
        raise ErrorInfraestructura("respuesta sin 'choices': no es una respuesta de la API")
    c0 = ch[0]
    if not isinstance(c0, dict) or not isinstance(c0.get("message"), dict):
        raise ErrorInfraestructura("choices[0] sin 'message'")
    return c0["message"], (c0.get("finish_reason") or "")


def timings(d: dict) -> dict:
    """Metricas de rendimiento. Su AUSENCIA es un fallo de medida, no un cero.

    Se acepta tanto la posicion de llama.cpp (raiz) como bajo 'usage'.
    """
    t = d.get("timings") or (d.get("usage") or {}).get("timings")
    if not isinstance(t, dict) or not t:
        raise ErrorInfraestructura("la respuesta no trae 'timings': medida invalida")
    faltan = [k for k in ("prompt_n", "prompt_per_second", "predicted_per_second")
              if not isinstance(t.get(k), (int, float)) or isinstance(t.get(k), bool)]
    if faltan:
        raise ErrorInfraestructura(f"timings incompletos, faltan {faltan}")
    # Que el campo sea numerico NO acredita una medida: NaN, inf, negativos y
    # recuentos fraccionarios pasaban la comprobacion de tipo y entraban en la
    # mediana. Se exige finitud, signo y que los recuentos sean enteros.
    for k in ("prompt_per_second", "predicted_per_second"):
        v = float(t[k])
        if not math.isfinite(v):
            raise ErrorInfraestructura(f"{k} no es finito ({t[k]!r}): medida invalida")
        if v <= 0:
            raise ErrorInfraestructura(f"{k}={t[k]!r} no es una velocidad plausible")
        if v > LIMITE_TASA:
            raise ErrorInfraestructura(
                f"{k}={t[k]!r} supera el limite de plausibilidad ({LIMITE_TASA} t/s): "
                "sospecha de reutilizacion de cache o de metrica corrupta")
    for k in ("prompt_n", "predicted_n"):
        if k not in t:
            continue
        v = t[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ErrorInfraestructura(f"{k}={v!r} no es un recuento")
        if not math.isfinite(float(v)) or float(v) != int(v):
            raise ErrorInfraestructura(f"{k}={v!r} no es un recuento entero")
        if int(v) <= 0:
            raise ErrorInfraestructura(f"{k}={v!r} no es un recuento positivo")
    return t


# ---------------------------------------------------------------- contratos


def respuesta_final(d: dict, exigir_stop: bool = False) -> str:
    """Contrato de TAREA: hace falta una respuesta final entregable.

    Rechaza contenido vacio (bucle de razonamiento, H-019) y finalizacion por
    agotamiento del presupuesto: al cliente le llega una respuesta truncada.

    `exigir_stop=True` endurece el contrato para las tareas de resultado
    comprobable: ademas de rechazar una finalizacion anormal, rechaza que el
    campo VENGA AUSENTE. Un finish_reason que falta no es una finalizacion
    normal, es una respuesta que no acredita como termino; darla por buena era
    rellenar el hueco con 'stop' por nuestra cuenta.
    """
    msg, fin = _eleccion(d)
    content = (msg.get("content") or "").strip()
    if not content:
        razon = (msg.get("reasoning_content") or "").strip()
        if razon:
            raise FalloContrato(
                f"content vacio con {len(razon)} chars de razonamiento "
                "(bucle de razonamiento, H-019)")
        raise FalloContrato("content vacio y sin razonamiento")
    if exigir_stop and not fin:
        raise FalloContrato(
            "falta finish_reason: no acredita finalizacion normal")
    if fin and fin != "stop":
        raise FalloContrato(f"finalizacion anormal: finish_reason={fin!r}")
    return content


def respuesta_exacta(d: dict, esperado: str) -> str:
    """Contrato de TAREA con resultado comprobable: el contenido ES el esperado.

    Contrato ESTRICTO, el mismo que promete docs/metodologia.md:
    content.strip() == esperado y finish_reason == 'stop'.

    No hay normalizacion permisiva. La version anterior colapsaba espacios y
    hacia rstrip('.'), con lo que '391.' y '391...' pasaban como '391' mientras
    la metodologia prometia comparacion exacta: dos contratos distintos, uno
    documentado y otro implementado. Si algun dia hace falta tolerar puntuacion,
    va en un contrato aparte y documentado, no escondido aqui.
    """
    content = respuesta_final(d, exigir_stop=True)
    if content != esperado:
        raise FalloContrato(f"esperaba {esperado!r} y llego {content[:120]!r}")
    return content


def generacion_medida(d: dict, max_tokens: int) -> dict:
    """Contrato de MICROBENCHMARK: se mide velocidad, no se completa una tarea.

    Aqui finish_reason='length' es la terminacion PREVISTA (el presupuesto de
    salida es el que fija la longitud medida) y se admite explicitamente, pero
    se exigen timings validos y tokens realmente generados, y se devuelve el
    recuento para que la tabla pueda declararlo. Nunca se presenta como tarea
    completada.
    """
    msg, fin = _eleccion(d)
    t = timings(d)
    generados = t.get("predicted_n")
    if not isinstance(generados, (int, float)) or generados <= 0:
        raise ErrorInfraestructura(f"no se generaron tokens (predicted_n={generados!r})")
    if fin not in ("", "stop", "length"):
        raise ErrorInfraestructura(f"finalizacion inesperada en un benchmark: {fin!r}")
    return {
        "prompt_n": t["prompt_n"],
        "pp": t["prompt_per_second"],
        "tg": t["predicted_per_second"],
        "predicted_n": generados,
        "finish_reason": fin or "stop",
        "truncada": fin == "length",
        "presupuesto": max_tokens,
        "texto": (msg.get("content") or "").strip(),
    }


def llamada_herramienta(d: dict, nombre: str | None = None) -> dict:
    """Contrato de HERRAMIENTAS: vale una llamada valida, sin exigir texto.

    Un modelo que responde solo con tool_calls esta haciendo lo correcto:
    rechazarlo por 'content vacio' seria un falso negativo.
    """
    msg, fin = _eleccion(d)
    tc = msg.get("tool_calls")
    if not isinstance(tc, list) or not tc:
        raise FalloContrato("no hay tool_calls en la respuesta")
    fn = (tc[0] or {}).get("function") or {}
    if not fn.get("name"):
        raise FalloContrato("tool_call sin nombre de funcion")
    if nombre and fn["name"] != nombre:
        raise FalloContrato(f"esperaba la herramienta {nombre!r} y llamo a {fn['name']!r}")
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except Exception as e:
        raise FalloContrato(f"argumentos no son JSON valido: {e}") from e
    if fin and fin not in ("tool_calls", "stop"):
        raise FalloContrato(f"finalizacion anormal con tool_calls: {fin!r}")
    return {"nombre": fn["name"], "argumentos": args}


def recuperacion_aguja(d: dict, clave: str) -> dict:
    """Contrato de RECUPERACION: la aguja es una tarea de resultado comprobable.

    Se separa a proposito de `generacion_medida`: usar el contrato de
    microbenchmark para la aguja aceptaba como buena una respuesta truncada por
    presupuesto, que es exactamente el caso en que el modelo NO ha llegado a
    decir la clave. Aqui la finalizacion tiene que ser normal.

    Se exige que la clave sea la respuesta, no que aparezca dentro de ella:
    la comprobacion por subcadena da por bueno "no encuentro la clave R7-K2"
    porque la cita. Se tolera unicamente puntuacion final y comillas, porque el
    modelo responde en prosa minima, y eso queda declarado aqui.
    """
    content = respuesta_final(d, exigir_stop=True)
    limpio = content.strip().strip("\"'`").rstrip(".!").strip()
    if limpio != clave:
        cita = clave in content
        e = FalloContrato(
            f"esperaba exactamente {clave!r} y llego {content[:120]!r}"
            + (" (la cita, pero no la entrega como respuesta)" if cita else ""))
        # Distingue los dos fallos, que NO significan lo mismo: citar la clave
        # envuelta en prosa es un fallo de formato con la recuperacion hecha;
        # no citarla es no haber leido la ventana. Quien analiza necesita
        # separarlos, asi que el motivo viaja en la excepcion.
        e.motivo = "formato" if cita else "no_recupera"
        e.clave_presente = cita
        raise e
    return {"clave": clave, "texto": content, "motivo": None}
