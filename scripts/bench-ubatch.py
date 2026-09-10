#!/usr/bin/env python3
"""Barrido de --ubatch-size sobre una unidad systemd DE PRUEBAS, no la productiva.

Historia de correcciones:

  H-021: la version original reescribia --batch-size y --ubatch-size con el
  mismo valor, asi que nunca aislo el efecto de ubatch. Ahora batch se fija con
  --batch y solo se mueve ubatch.

  H-024 (auditoria externa del corte 35a2f26), corregido aqui:
    a) `espera_salud(puerto)` aceptaba un puerto pero SIEMPRE consultaba
       `systemctl is-active llama-flashnext-bench`. En la restauracion final se
       la llamaba con 8080: si la productiva no levantaba, la unidad de pruebas
       (ya parada y borrada) reportaba 'inactive', no 'failed', y la funcion se
       quedaba dando vueltas 600 s antes de decir False. Ahora recibe unidad Y
       puerto y consulta la unidad que le corresponde.
    b) Una respuesta 200 con cuerpo `{}` producia (0, 0, 0) y la fila se
       marcaba 'ok'. Ahora la validacion vive en validacion.py y una medida sin
       timings es un error, no un cero.
    c) El proceso salia con codigo 0 aunque todos los puntos hubieran fallado.
       Ahora el codigo de salida refleja el resultado (ver TABLA DE SALIDAS).
    d) La restauracion "intentaba arrancar" la productiva sin comprobar que
       quedaba sana, y el resumen final no lo destacaba. Ahora un fallo de
       restauracion es la condicion de salida MAS grave y se grita en pantalla.
    e) Sin calentamiento: la primera pasada pagaba la carga en frio del modelo.
       Ahora hay una pasada de calentamiento explicita que se registra y NO
       entra en la mediana.
    f) Solo quedaba el agregado. Ahora cada peticion se escribe en un JSONL
       crudo y los resumenes se calculan desde ese registro.

TABLA DE SALIDAS:
    0  todos los puntos medidos y productiva restaurada
    1  error de uso o de entorno (falta clave, falta sudo, unidad ilegible)
    2  algun punto del barrido fallo (no arranco o no se pudo medir)
    3  FALLO DE RESTAURACION: la productiva no volvio a estado sano

Uso: sudo -v primero.
    LLAMA_API_KEY=... python3 bench-ubatch.py --ubatch 512 1024 2048 --batch 4096
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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validacion import (ErrorInfraestructura, FalloContrato,  # noqa: E402
                        cuerpo_json, generacion_medida)

UNIT_PROD = "llama-flashnext"
UNIT_BENCH = "llama-flashnext-bench"
RUTA_BENCH = f"/etc/systemd/system/{UNIT_BENCH}.service"
PUERTO_PROD = 8080
PUERTO_BENCH = 8081
CLAVE = os.environ.get("LLAMA_API_KEY", "")

SALIDA_OK, SALIDA_ENTORNO, SALIDA_MEDIDA, SALIDA_RESTAURACION = 0, 1, 2, 3


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"fallo: {cmd}\n{r.stderr.strip()[:300]}")
    return r.stdout.strip()


def unidad_productiva():
    return sh(f"sudo -n cat /etc/systemd/system/{UNIT_PROD}.service")


def batch_de_referencia(texto):
    """Lee --batch-size de la unidad PRODUCTIVA.

    H-027: el piloto se lanzo con --batch 2048 mientras produccion corria con
    4096, asi que las cifras no eran comparables con la linea base. El valor
    se habia escrito a mano. Ahora la referencia se LEE de la unidad y quien
    quiera apartarse de ella tiene que decirlo en voz alta (--batch).
    """
    m = re.search(r"--batch-size\s+(\d+)", texto)
    if not m:
        raise RuntimeError("no encontre --batch-size en la unidad productiva")
    return int(m.group(1))


def construye_unidad(texto, ubatch, batch):
    """Deriva la unidad de pruebas. ubatch y batch se fijan por separado."""
    t = texto
    t = re.sub(r"--ubatch-size\s+\d+", f"--ubatch-size {ubatch}", t)
    t = re.sub(r"--batch-size\s+\d+", f"--batch-size {batch}", t)
    if f"--ubatch-size {ubatch}" not in t:
        raise RuntimeError("no encontre --ubatch-size en la unidad productiva")
    if f"--batch-size {batch}" not in t:
        raise RuntimeError("no encontre --batch-size en la unidad productiva")
    t = t.replace(f"--port {PUERTO_PROD}", f"--port {PUERTO_BENCH}")
    if f"--port {PUERTO_BENCH}" not in t:
        raise RuntimeError(f"no encontre --port {PUERTO_PROD} en la unidad productiva")
    t = t.replace(
        "Description=llama.cpp server",
        f"Description=[BENCH ubatch={ubatch} batch={batch}] llama.cpp server",
    )
    # sin reinicio automatico: si un ubatch no arranca, quiero verlo, no un bucle
    t = re.sub(r"Restart=on-failure", "Restart=no", t)
    return t


def espera_salud(unidad, puerto, limite=600):
    """Espera a que ESA unidad sirva /health en ESE puerto.

    Recibe las dos cosas a proposito: consultar una unidad distinta de la que
    escucha en el puerto fue el fallo de encaminamiento que corrige H-024.
    Corta antes de tiempo si la unidad entra en 'failed' o si desaparece.

    Exige AMBAS condiciones a la vez, y ese orden importa: comprobar el HTTP
    primero y devolver True con un 200 permitia que "algo responde en este
    puerto" se confundiera con "he restaurado esta unidad" -- la unidad podia
    estar 'failed' y la funcion no consultaba systemd ni una vez. Primero la
    unidad activa, y solo entonces /health.
    """
    t0 = time.time()
    visto_activo = False
    while time.time() - t0 < limite:
        estado = sh(f"systemctl is-active {unidad}", check=False)
        if estado == "active":
            visto_activo = True
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/health", timeout=5) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
        elif estado in ("failed", "inactive"):
            arrancando = sh(f"systemctl show -p ActiveState --value {unidad}",
                            check=False) == "activating"
            if not arrancando:
                print(f"    [!] {unidad} en estado {estado!r}, dejo de esperar")
                return False
        time.sleep(5)
    if visto_activo:
        print(f"    [!] {unidad} activa pero /health no respondio 200 en {limite} s")
    else:
        print(f"    [!] {unidad} no llego a 'active' en {limite} s")
    return False


def una_peticion(puerto, prompt, max_tokens, timeout=1200):
    """Una peticion validada. Distingue infraestructura de contrato."""
    cuerpo = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0, "cache_prompt": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{puerto}/v1/chat/completions", data=cuerpo,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {CLAVE}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                raise ErrorInfraestructura(f"HTTP {r.status}")
            bruto = r.read()
    except urllib.error.HTTPError as e:
        raise ErrorInfraestructura(f"HTTP {e.code}: {e.read()[:200]!r}") from e
    except urllib.error.URLError as e:
        raise ErrorInfraestructura(f"sin respuesta del servidor: {e.reason}") from e
    except TimeoutError as e:
        raise ErrorInfraestructura(f"timeout tras {timeout} s") from e
    m = generacion_medida(cuerpo_json(bruto), max_tokens)
    m["wall"] = time.time() - t0
    return m


def mide(puerto, palabras, pasadas, ubatch, batch, jsonl, max_tokens=160,
         reintentos_calentamiento=1, batch_ref=None):
    """Calentamiento + N pasadas medidas. Devuelve solo las medidas.

    El calentamiento se registra en el JSONL con warmup=true para que quede
    rastro, pero NO entra en ninguna mediana: la primera pasada paga la carga
    en frio de pesos y la reserva del KV cache.

    Un calentamiento que falla ABORTA el punto (con una recuperacion limitada
    y explicita, `reintentos_calentamiento`). Antes solo se propagaba el fallo
    de las pasadas medidas, asi que si reventaba el calentamiento y respondian
    las dos siguientes, el punto se publicaba como 'ok' con salida 0: la
    primera medida contabilizada estaba pagando el trabajo en frio que el
    protocolo dice excluir. El fallo quedaba en el JSONL, pero no en el
    resumen, que es lo que se lee.
    """
    prompt = (("El sistema de control industrial registra temperaturas del horno. "
               * palabras)[:200000] + "\n\nResume en una frase.")
    medidas = []
    calentado = False
    intentos_w = 0
    i = 0
    while i < pasadas + 1:
        es_warmup = not calentado
        if es_warmup:
            intentos_w += 1
            etiqueta = ("calentamiento" if intentos_w == 1
                        else f"calentamiento (reintento {intentos_w - 1})")
        else:
            etiqueta = f"pasada {len(medidas) + 1}/{pasadas}"
        try:
            m = una_peticion(puerto, prompt, max_tokens)
            registro = dict(m, ubatch=ubatch, batch=batch, warmup=es_warmup,
                            fase="calentamiento" if es_warmup else "medida",
                            pasada=None if es_warmup else len(medidas) + 1,
                            batch_ref=batch_ref,
                            comparable_con_produccion=(batch_ref is None
                                                       or batch == batch_ref),
                            intento=intentos_w if es_warmup else None,
                            ts=datetime.now(timezone.utc).isoformat(), fallo=None)
            print(f"      {etiqueta}: prompt_n={m['prompt_n']} pp={m['pp']:.1f} "
                  f"tg={m['tg']:.2f} gen={m['predicted_n']} fin={m['finish_reason']}")
            if es_warmup:
                calentado = True
            else:
                medidas.append(m)
        except (ErrorInfraestructura, FalloContrato) as e:
            registro = {"ubatch": ubatch, "batch": batch, "warmup": es_warmup,
                        "fase": "calentamiento" if es_warmup else "medida",
                        "pasada": None if es_warmup else len(medidas) + 1,
                        "batch_ref": batch_ref,
                        "comparable_con_produccion": (batch_ref is None
                                                      or batch == batch_ref),
                        "intento": intentos_w if es_warmup else None,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "fallo": f"{type(e).__name__}: {e}"}
            print(f"      {etiqueta}: FALLO {type(e).__name__}: {str(e)[:120]}")
        jsonl.write(json.dumps(registro, ensure_ascii=False) + "\n")
        jsonl.flush()
        # Las peticiones fallidas se CONSERVAN en el registro y cuentan en la
        # tasa de fallos: no se repite hasta juntar N buenas (eso sesga).
        if registro["fallo"]:
            if not es_warmup:
                raise RuntimeError(registro["fallo"])
            if intentos_w > reintentos_calentamiento:
                raise RuntimeError(
                    f"calentamiento fallido tras {intentos_w} intento(s), "
                    f"abandono el punto ubatch={ubatch}: sin calentamiento "
                    f"completado las medidas no son comparables "
                    f"({registro['fallo']})")
            continue  # reintento acotado del calentamiento, sin contar medida
        i += 1
    if not calentado:
        raise RuntimeError("no hubo calentamiento completado")
    if len(medidas) != pasadas:
        raise RuntimeError(f"esperaba {pasadas} medidas y hay {len(medidas)}")
    return medidas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ubatch", type=int, nargs="+", default=[512, 1024, 2048])
    ap.add_argument("--batch", type=int, default=None,
                    help="fijo en todo el barrido; por defecto se LEE de la "
                         "unidad productiva para que las cifras sean "
                         "comparables con la linea base (H-027)")
    ap.add_argument("--palabras", type=int, default=3000)
    ap.add_argument("--passes", type=int, default=5,
                    help="pasadas MEDIDAS; ademas se hace una de calentamiento")
    ap.add_argument("--jsonl", default="",
                    help="registro crudo por peticion (por defecto benchmarks/crudo-<ts>.jsonl)")
    a = ap.parse_args()

    if not CLAVE:
        print("Falta LLAMA_API_KEY", file=sys.stderr)
        return SALIDA_ENTORNO
    if sh("id -u") != "0" and sh("sudo -n true; echo $?", check=False) != "0":
        print("Necesito sudo sin contrasena (ejecuta 'sudo -v' antes)", file=sys.stderr)
        return SALIDA_ENTORNO
    try:
        prod = unidad_productiva()
    except RuntimeError as e:
        print(f"No puedo leer la unidad productiva: {e}", file=sys.stderr)
        return SALIDA_ENTORNO

    ref = batch_de_referencia(prod)
    if a.batch is None:
        a.batch = ref
        aviso_batch = f"[i] batch FIJO en {a.batch} (leido de la unidad productiva)"
    elif a.batch != ref:
        aviso_batch = (f"[!] batch {a.batch} DISTINTO del productivo ({ref}): "
                       f"las cifras NO son comparables con la linea base")
    else:
        aviso_batch = f"[i] batch FIJO en {a.batch} (coincide con produccion)"

    ruta_jsonl = a.jsonl or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "benchmarks",
        f"crudo-ubatch-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(ruta_jsonl)), exist_ok=True)

    prod_estaba_activa = sh(f"systemctl is-active {UNIT_PROD}", check=False) == "active"
    print(f"[i] productiva activa al empezar: {prod_estaba_activa}")
    print(aviso_batch)
    print(f"[i] {a.passes} pasadas medidas + 1 de calentamiento descartada")
    print(f"[i] registro crudo: {os.path.abspath(ruta_jsonl)}\n")

    filas = []
    restauracion_ok = True
    with open(ruta_jsonl, "a", encoding="utf-8") as jsonl:
        try:
            if prod_estaba_activa:
                print(f"[i] parando {UNIT_PROD} (la GPU no da para dos servidores)")
                sh(f"sudo -n systemctl stop {UNIT_PROD}")

            for ub in a.ubatch:
                print(f"[*] ubatch={ub} (batch={a.batch})")
                sh(f"sudo -n tee {RUTA_BENCH} >/dev/null <<'EOF'\n"
                   f"{construye_unidad(prod, ub, a.batch)}\nEOF")
                sh("sudo -n systemctl daemon-reload")
                sh(f"sudo -n systemctl restart {UNIT_BENCH}", check=False)
                if not espera_salud(UNIT_BENCH, PUERTO_BENCH):
                    print("    [X] no arranco o murio - lo anoto y sigo")
                    filas.append((ub, None, None, None, "no arranca"))
                    sh(f"sudo -n systemctl stop {UNIT_BENCH}", check=False)
                    continue
                try:
                    ms = mide(PUERTO_BENCH, a.palabras, a.passes, ub, a.batch,
                                  jsonl, batch_ref=ref)
                    filas.append((ub, ms[0]["prompt_n"],
                                  statistics.median(m["pp"] for m in ms),
                                  statistics.median(m["tg"] for m in ms), "ok"))
                except Exception as e:
                    print(f"    [X] fallo midiendo: {str(e)[:150]}")
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
                restauracion_ok = espera_salud(UNIT_PROD, PUERTO_PROD, limite=300)
                if restauracion_ok:
                    print(f"[i] {UNIT_PROD} restaurada y sirviendo en {PUERTO_PROD}")
                else:
                    estado = sh(f"systemctl is-active {UNIT_PROD}", check=False)
                    print("\n" + "=" * 66, file=sys.stderr)
                    print(f"FALLO DE RESTAURACION: {UNIT_PROD} estaba activa al empezar "
                          f"y ahora esta {estado!r} sin responder en {PUERTO_PROD}.",
                          file=sys.stderr)
                    print(f"Revisa: journalctl -u {UNIT_PROD} -n 50", file=sys.stderr)
                    print("=" * 66, file=sys.stderr)
            # la unidad productiva nunca se toco: se leyo, no se escribio
            print(f"[i] unidad productiva intacta: {'--api-key-file' in prod}")

    print("\n| ubatch | batch | prompt_n | pp t/s | tg t/s | estado |")
    print("|---|---|---|---|---|---|")
    for ub, pn, pp, tg, est in filas:
        if pp is None:
            print(f"| {ub} | {a.batch} | - | - | - | {est} |")
        else:
            print(f"| {ub} | {a.batch} | {pn} | {pp:.1f} | {tg:.2f} | {est} |")
    print(f"\nRegistro crudo por peticion: {os.path.abspath(ruta_jsonl)}")

    fallidos = [f for f in filas if f[4] != "ok"]
    if not restauracion_ok:
        return SALIDA_RESTAURACION
    if fallidos or not filas:
        print(f"[!] {len(fallidos)}/{len(filas)} puntos fallaron", file=sys.stderr)
        return SALIDA_MEDIDA
    return SALIDA_OK


if __name__ == "__main__":
    sys.exit(main())
