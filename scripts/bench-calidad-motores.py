#!/usr/bin/env python3
"""Bateria de calidad comparable entre dos servidores OpenAI-compatibles.

No juzga "suena mejor": cada prompt trae un verificador objetivo en Python.
Escribe un JSONL por motor; la comparacion se hace luego cargando los dos.

Uso:
  calidad-gufo-vs-llamacpp.py --base http://127.0.0.1:8081/v1 \
      --model "Qwen3.8 Flash Next" --etiqueta gufo --salida /tmp/cal-gufo.jsonl
  calidad-gufo-vs-llamacpp.py --base http://127.0.0.1:8080/v1 \
      --model qwen3.8-flash-next --key-file /etc/llama-server/flash-next.env \
      --etiqueta llamacpp --salida /tmp/cal-llamacpp.jsonl
"""
import argparse
import json
import re
import uuid
import subprocess
import sys
import tempfile
import time
import urllib.request
import os


# ---------------------------------------------------------------- verificadores

def v_json_valido(texto, esperado=None):
    """Extrae el primer objeto JSON del texto y comprueba claves/valores."""
    crudo = texto.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", crudo, re.S)
    if m:
        crudo = m.group(1).strip()
    inicio = crudo.find("{")
    fin = crudo.rfind("}")
    if inicio == -1 or fin == -1:
        return False, "no hay objeto JSON"
    try:
        d = json.loads(crudo[inicio:fin + 1])
    except Exception as e:
        return False, "json invalido: %s" % e
    if esperado:
        for k, val in esperado.items():
            if k not in d:
                return False, "falta clave %s" % k
            if val is not None and d[k] != val:
                return False, "clave %s = %r, esperado %r" % (k, d[k], val)
    return True, "ok (%d claves)" % len(d)


def _bloque_codigo(texto, lang):
    m = re.search(r"```(?:%s)?\s*(.*?)```" % lang, texto, re.S)
    return m.group(1).strip() if m else texto.strip()


def v_python_ejecuta(texto, comprobacion):
    """Escribe el codigo, lo ejecuta y compara stdout con lo esperado."""
    codigo = _bloque_codigo(texto, "python")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(codigo + "\n" + comprobacion)
        ruta = f.name
    try:
        r = subprocess.run([sys.executable, ruta], capture_output=True,
                           text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "timeout ejecutando"
    finally:
        os.unlink(ruta)
    if r.returncode != 0:
        return False, "error: %s" % (r.stderr.strip()[:200] or "rc!=0")
    if "OK" not in r.stdout:
        return False, "stdout sin OK: %r" % r.stdout.strip()[:120]
    return True, "ejecuta y pasa asserts"


def v_contiene(texto, agujas, todas=True):
    falt = [a for a in agujas if a.lower() not in texto.lower()]
    if todas and falt:
        return False, "faltan: %s" % ", ".join(falt[:4])
    if not todas and len(falt) == len(agujas):
        return False, "no aparece ninguna"
    return True, "ok"


def v_no_contiene(texto, agujas):
    hay = [a for a in agujas if a.lower() in texto.lower()]
    if hay:
        return False, "aparece lo prohibido: %s" % ", ".join(hay[:4])
    return True, "ok"


def v_lineas_exactas(texto, n):
    ls = [l for l in texto.strip().splitlines() if l.strip()]
    if len(ls) != n:
        return False, "%d lineas, esperadas %d" % (len(ls), n)
    return True, "ok"


def limpia_think(texto):
    """Quita bloques <think>...</think> y un <think> abierto sin cierre (truncado)."""
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL)
    texto = re.sub(r"<think>.*$", "", texto, flags=re.DOTALL)
    return texto.strip()


def v_castellano(texto):
    palabras = set(re.findall(r"\b[a-záéíóúüñ]+\b", texto.lower()))
    marcas = {"el", "la", "los", "las", "un", "una", "de", "del", "al", "que",
              "en", "con", "por", "para", "es", "son", "se", "como"}
    hits = len(palabras & marcas)
    if hits < 3:
        return False, "no parece castellano (%d marcas)" % hits
    return True, "ok (%d marcas)" % hits


# ---------------------------------------------------------------------- pruebas

AGUJA = ("El codigo de autorizacion del quemador auxiliar de la camara limpia "
         "es ZX-4417-QD y solo lo conoce el responsable de pruebas.")


def _texto_largo(aguja_en, repeticiones=1500):
    """Texto de relleno con una aguja escondida a cierta profundidad."""
    bloque = ("El sistema de control del horno industrial regula la temperatura "
              "mediante lazos PID en cascada y vigila la concentracion de oxigeno. ")
    partes = [bloque] * repeticiones
    partes.insert(aguja_en, AGUJA + " ")
    return "".join(partes)


PRUEBAS = [
    {
        "id": "json-estructura",
        "prompt": ("Devuelve EXCLUSIVAMENTE un objeto JSON, sin texto alrededor, "
                   "con estas claves exactas: nombre (string, valor \"horno-A\"), "
                   "temperatura_c (numero, valor 1250), activo (booleano, valor true), "
                   "alarmas (lista vacia)."),
        "verif": lambda t: v_json_valido(t, {"nombre": "horno-A",
                                             "temperatura_c": 1250,
                                             "activo": True,
                                             "alarmas": []}),
    },
    {
        "id": "json-anidado",
        "prompt": ("Devuelve solo JSON valido que represente dos sensores. Clave raiz "
                   "\"sensores\", lista de dos objetos, cada uno con \"id\" (string) y "
                   "\"lectura\" (numero). Sin markdown ni explicacion."),
        "verif": lambda t: v_json_valido(t, {"sensores": None}),
    },
    {
        "id": "codigo-python-binaria",
        "prompt": ("Escribe una funcion Python `busca(lista, objetivo)` que haga busqueda "
                   "binaria sobre una lista ordenada y devuelva el indice, o -1 si no esta. "
                   "Solo el bloque de codigo, sin explicacion."),
        "verif": lambda t: v_python_ejecuta(t, (
            "\nassert busca([1,3,5,7,9,11], 7) == 3\n"
            "assert busca([1,3,5,7,9,11], 1) == 0\n"
            "assert busca([1,3,5,7,9,11], 11) == 5\n"
            "assert busca([1,3,5,7,9,11], 4) == -1\n"
            "assert busca([], 1) == -1\n"
            "print('OK')\n")),
    },
    {
        "id": "codigo-python-parser",
        "prompt": ("Escribe una funcion Python `parsea_duracion(s)` que convierta cadenas "
                   "como '1h30m', '45m', '2h', '90s' en segundos (entero). Debe lanzar "
                   "ValueError si la cadena no encaja. Solo el bloque de codigo."),
        "verif": lambda t: v_python_ejecuta(t, (
            "\nassert parsea_duracion('1h30m') == 5400\n"
            "assert parsea_duracion('45m') == 2700\n"
            "assert parsea_duracion('2h') == 7200\n"
            "assert parsea_duracion('90s') == 90\n"
            "try:\n"
            "    parsea_duracion('manzana')\n"
            "    raise AssertionError('deberia lanzar')\n"
            "except ValueError:\n"
            "    pass\n"
            "print('OK')\n")),
    },
    {
        "id": "codigo-python-estado",
        "prompt": ("Escribe una clase Python `Contador` con metodos `suma(n)`, `resta(n)` "
                   "y propiedad `valor`, que nunca baje de cero (si una resta lo dejaria "
                   "negativo, queda en 0). Solo el bloque de codigo."),
        "verif": lambda t: v_python_ejecuta(t, (
            "\nc = Contador()\n"
            "c.suma(5); assert c.valor == 5\n"
            "c.resta(2); assert c.valor == 3\n"
            "c.resta(99); assert c.valor == 0\n"
            "print('OK')\n")),
    },
    {
        "id": "instruccion-formato",
        "prompt": ("Enumera exactamente tres ventajas de los lazos PID en cascada. "
                   "Una por linea, cada linea empezando por '- '. Sin titulo, sin "
                   "introduccion, sin conclusion."),
        "verif": lambda t: v_lineas_exactas(t, 3),
    },
    {
        "id": "instruccion-negativa",
        "prompt": ("Explica en dos frases que es un termopar tipo K. Prohibido usar las "
                   "palabras 'temperatura', 'calor' y 'grados'."),
        "verif": lambda t: v_no_contiene(t, ["temperatura", "calor", "grados"]),
    },
    {
        "id": "idioma-castellano",
        "prompt": "Explain in Spanish what a PLC is. Answer only in Spanish.",
        "verif": v_castellano,
    },
    {
        "id": "razonamiento-aritmetico",
        "prompt": ("Un horno consume 420 kW durante 7 horas y 180 kW durante otras 5 horas. "
                   "Si el kWh cuesta 0,18 euros, cuanto cuesta en total? Responde con el "
                   "numero final en euros al final del mensaje."),
        # 420*7 + 180*5 = 2940 + 900 = 3840 kWh * 0,18 = 691,2
        "verif": lambda t: v_contiene(t, ["691"], todas=True),
    },
    {
        "id": "razonamiento-trampa",
        "prompt": ("Si tres maquinas tardan tres minutos en fabricar tres piezas, cuanto "
                   "tardan cien maquinas en fabricar cien piezas? Responde con el numero "
                   "de minutos."),
        # OJO: el modelo suele contestar solo "3" porque el prompt pide "el numero
        # de minutos". Exigir la cadena "3 minutos" daba un falso NO en los dos
        # motores (H-044). Se acepta el 3 suelto y se rechaza el 100 de la trampa.
        "verif": lambda t: (
            (False, "cae en la trampa: responde 100")
            if re.search(r"\b100\b", t) else
            (True, "ok")
            if re.search(r"\b(3|tres)\b", t) else
            (False, "no aparece ni 3 ni tres")
        ),
    },
    {
        "id": "aguja-contexto-medio",
        "prompt": None,  # se construye abajo
        "construye": lambda: _texto_largo(750) + (
            "\n\nPregunta: cual es el codigo de autorizacion del quemador auxiliar "
            "de la camara limpia? Responde solo con el codigo."),
        "verif": lambda t: v_contiene(t, ["ZX-4417-QD"]),
    },
    {
        "id": "aguja-contexto-final",
        "prompt": None,
        "construye": lambda: _texto_largo(1450) + (
            "\n\nPregunta: quien conoce el codigo de autorizacion del quemador "
            "auxiliar? Responde en una frase."),
        "verif": lambda t: v_contiene(t, ["responsable"]),
    },
]


# ------------------------------------------------------------------------ motor

def pide(base, model, key, prompt, max_tokens=900, timeout=1800):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    cab = {"Content-Type": "application/json"}
    if key:
        cab["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=cab)
    t0 = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    dt = time.time() - t0
    msg = r["choices"][0]["message"]
    return msg.get("content") or "", r.get("usage", {}), dt


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--key-file")
    p.add_argument("--etiqueta", required=True)
    p.add_argument("--salida", required=True)
    p.add_argument("--bust-cache", action="store_true",
                   help="antepone un run-id unico a cada prompt para invalidar la cache de prefijos")
    a = p.parse_args()

    # La clave puede venir por entorno (MOTOR_KEY) para no exigir permisos de
    # lectura sobre /etc/llama-server/*.env, que es root-only.
    key = os.environ.get("MOTOR_KEY") or None
    if not key and a.key_file:
        with open(a.key_file) as f:
            for linea in f:
                if linea.startswith("LLAMA_API_KEY="):
                    key = linea.split("=", 1)[1].strip()

    pasa = 0
    with open(a.salida, "w") as out:
        for t in PRUEBAS:
            prompt = t["prompt"] if t["prompt"] else t["construye"]()
            if a.bust_cache:
                prompt = "[run-id: %s]\n" % uuid.uuid4().hex[:8] + prompt
            try:
                texto, usage, dt = pide(a.base, a.model, key, prompt)
            except Exception as e:
                fila = {"id": t["id"], "motor": a.etiqueta, "ok": False,
                        "motivo": "fallo peticion: %s" % e, "segundos": None}
                out.write(json.dumps(fila, ensure_ascii=False) + "\n")
                out.flush()
                print("[FALLO] %-26s %s" % (t["id"], e))
                continue
            texto_limpio = limpia_think(texto)
            ok, motivo = t["verif"](texto_limpio)
            pasa += 1 if ok else 0
            fila = {
                "id": t["id"], "motor": a.etiqueta, "ok": ok, "motivo": motivo,
                "segundos": round(dt, 2),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "vacio": not texto_limpio.strip(),
                "respuesta": texto[:4000],
            }
            out.write(json.dumps(fila, ensure_ascii=False) + "\n")
            out.flush()
            print("[%s] %-26s %6.1fs  %s" % ("OK " if ok else "NO ", t["id"], dt, motivo))

    print("\n%s: %d/%d pruebas pasadas" % (a.etiqueta, pasa, len(PRUEBAS)))


if __name__ == "__main__":
    main()
