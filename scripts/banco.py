#!/usr/bin/env python3
"""Servidor de banco: levantar un llama-server de laboratorio con la linea de
arranque REAL de produccion, medirlo, y matarlo sin dejar nada colgando.

Por que existe, y por que no vale con "lanza el binario con unos flags":

  1. **La linea de arranque se DERIVA de la unidad, no se reescribe a mano.**
     Una campana que mide con una linea de argumentos copiada a mano mide otra
     configuracion distinta de la que corre en produccion, y la conclusion no
     se puede trasladar. `args_de_unidad()` parsea el ExecStart efectivo (el
     mismo criterio que `credencial.sh`: continuaciones unidas, prefijos
     `-+@!:` fuera, ultimo ExecStart no vacio) y `con_cambios()` toca SOLO el
     flag que la fase quiere mover.

  2. **La credencial no viaja por argv.** H-021: cualquier usuario local lee
     `/proc/<pid>/cmdline`. Si el binario declara `LLAMA_ARG_API_KEY` en su
     ayuda, la clave va por el entorno del hijo; si no, se escribe en un
     fichero temporal `600` que se borra en el `finally`. En ninguno de los dos
     casos aparece en la linea de comandos ni en el log.

  3. **El servidor de banco se mata SIEMPRE.** H-031 dejo claro lo que cuesta
     lo contrario: un servidor de banco mal apagado escuchando en el puerto es
     exactamente lo que hacia que `espera_prod` diera verde a "produccion
     restaurada" (fallo B). Aqui el cierre es SIGTERM al grupo de procesos,
     SIGKILL a los 20 s, y despues se espera a que el puerto quede LIBRE: que
     el proceso haya muerto no garantiza que el socket este cerrado.

  4. **La espera no es "algo responde en el puerto".** Se delega en
     `salud.espera_proceso`: proceso vivo Y /health 200 Y el modelo esperado en
     /v1/models. Un proceso que muere al arrancar es ErrorInfraestructura en el
     acto, no una espera que se come el limite entero.

  5. **El log del hijo y el anillo del kernel son medidas, no adorno.** H-034
     confirma que la especulacion esta activa leyendo `draft acceptance = ...`
     del propio servidor (`ServidorBanco.log_tail`), y vigila los resets de GPU
     de #27306 con `dmesg_desde()`. Por eso la salida del hijo ya nunca va a
     /dev/null, y por eso `dmesg_desde` devuelve **None** cuando no ha podido
     mirar: "no lo se" y "no ha pasado nada" no son lo mismo.

Uso tipico desde una fase de `campana.py`:

    args = banco.con_cambios(banco.args_de_unidad(texto_unidad),
                             cache_ram=12288, parallel=2, kvu=True)
    with banco.ServidorBanco(ctx.binario("baseline"), args, ctx.puerto_banco,
                             modelo=ctx.modelo, clave=banco.clave_de(ctx),
                             entorno=ctx.entorno_con_clave(),
                             log=ctx.log, dir_log=ctx.dir_salida) as srv:
        d = banco.peticion_chat(srv.url, banco.clave_de(ctx), mensajes,
                                max_tokens=64, temperature=0)
        print(banco.ttft_ms(d), srv.rss_mb())
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import salud  # noqa: E402
from validacion import ErrorInfraestructura, cuerpo_json, timings  # noqa: E402

# Todos por entorno para que las pruebas no tengan que esperar minutos reales.
# Se leen en cada construccion, no al importar: una prueba puede cambiarlos.
LIMITE_ARRANQUE = 900.0        # s hasta declarar que el servidor no arranca
PAUSA_ESPERA = 2.0             # s entre sondeos de /health
GRACIA_CIERRE = 20.0           # s entre el SIGTERM y el SIGKILL
JOURNALCTL = "journalctl"      # se sustituye por entorno en las pruebas
SUDO = "sudo -n"               # idem; vaciarlo desactiva el segundo intento


# ====================================================================== unidad
def _execstart_efectivo(texto: str) -> tuple[str, str]:
    """ExecStart efectivo del texto de una unidad: (linea, prefijos).

    Mismas reglas que `credencial.sh::_execstart_efectivo`, y a proposito: si
    los dos leyeran la unidad de forma distinta, la clave saldria de una linea
    de arranque y los argumentos de otra. Las continuaciones `\\` se unen, los
    prefijos de systemd (`-+@!:`) se separan del binario, y si hay varios
    ExecStart (unidad base + drop-ins) manda el ULTIMO no vacio.
    """
    ultimo = prefijos_ultimo = ""
    acum = prefijos = ""
    continua = False
    for linea in texto.splitlines():
        if continua:
            cuerpo = linea.strip()
            sigue = cuerpo.endswith("\\")
            acum += " " + (cuerpo[:-1] if sigue else cuerpo).strip()
            if not sigue:
                continua = False
                if acum.strip():
                    ultimo, prefijos_ultimo = acum.strip(), prefijos
            continue
        s = linea.strip()
        if not s.startswith("ExecStart="):
            continue
        cuerpo = s[len("ExecStart="):].strip()
        prefijos = ""
        while cuerpo and cuerpo[0] in "-+@!:":
            prefijos += cuerpo[0]
            cuerpo = cuerpo[1:]
        sigue = cuerpo.endswith("\\")
        acum = (cuerpo[:-1] if sigue else cuerpo).strip()
        if sigue:
            continua = True
        elif acum:
            ultimo, prefijos_ultimo = acum, prefijos
    return ultimo, prefijos_ultimo


def args_de_unidad(texto_unidad: str) -> list[str]:
    """Argumentos del ExecStart, SIN el binario: lo que le pasariamos al banco.

    El binario se deja fuera a proposito: la fase elige cual usar
    (`ctx.binario("baseline"|"candidato")`), y el de la unidad es el del
    symlink productivo, que es justo el que NO queremos fijar aqui.
    """
    linea, prefijos = _execstart_efectivo(texto_unidad)
    if not linea:
        raise ErrorInfraestructura(
            "el texto de la unidad no declara ningun ExecStart no vacio: "
            "sin linea de arranque no hay configuracion productiva que medir")
    try:
        toks = shlex.split(linea)
    except ValueError as e:
        raise ErrorInfraestructura(
            f"el ExecStart no se puede trocear ({e}): comillas sin cerrar") from e
    if not toks:
        raise ErrorInfraestructura("el ExecStart esta vacio tras quitar prefijos")
    # Con el prefijo '@', systemd toma el segundo token como argv[0]: el
    # binario son DOS tokens y los argumentos empiezan en el tercero.
    return toks[2:] if "@" in prefijos else toks[1:]


# Nombre canonico primero: es el que se emite al anadir. Se usan las formas
# cortas que trae la unidad productiva (`-np`, `-kvu`) para que el diff entre
# la linea medida y la documentada sea legible.
_FLAGS = {
    "model":        (("--model", "-m"), "valor"),
    "port":         (("--port",), "valor"),
    "host":         (("--host",), "valor"),
    "cache_ram":    (("--cache-ram",), "valor"),
    "cache_reuse":  (("--cache-reuse",), "valor"),
    "parallel":     (("-np", "--parallel"), "valor"),
    "np":           (("-np", "--parallel"), "valor"),
    "ctx_size":     (("-c", "--ctx-size"), "valor"),
    "ubatch_size":  (("--ubatch-size", "-ub"), "valor"),
    "batch_size":   (("--batch-size", "-b"), "valor"),
    "api_key":      (("--api-key",), "valor"),
    "api_key_file": (("--api-key-file",), "valor"),
    "alias":        (("--alias",), "valor"),
    "mmproj":       (("--mmproj",), "valor"),
    "kvu":          (("-kvu", "--kv-unified"), "bandera"),
    "metrics":      (("--metrics",), "bandera"),
    # Especulacion sidecar (H-034). Solo las entiende la build con la PR
    # #28243 apilada; en la baseline son flags desconocidos y el servidor no
    # arranca, que es justo lo que queremos que pase si alguien las mezcla.
    "spec_type":        (("--spec-type",), "valor"),
    "draft_model":      (("-md", "--model-draft"), "valor"),
    "spec_draft_n_max": (("--spec-draft-n-max",), "valor"),
    "spec_draft_p_min": (("--spec-draft-p-min",), "valor"),
}


def _quita(args: list[str], nombres: tuple[str, ...], con_valor: bool) -> list[str]:
    fuera, i = [], 0
    while i < len(args):
        t = args[i]
        base = t.split("=", 1)[0]
        if base in nombres:
            i += 2 if (con_valor and "=" not in t) else 1
            continue
        fuera.append(t)
        i += 1
    return fuera


def _alias_de(flag: str) -> tuple[tuple[str, ...] | None, str | None]:
    """Alias y tipo de un flag TAL COMO APARECE EN LA LINEA (`-kvu`, `--mmproj`)."""
    for nombres, tipo in _FLAGS.values():
        if flag in nombres:
            return nombres, tipo
    return None, None


def _quita_desconocido(args: list[str], flag: str) -> list[str]:
    """Quita un flag que no esta en la tabla, y su valor si la linea dice que
    lo lleva.

    Sin tabla no hay forma de saber si `--loquesea` consume el token siguiente,
    asi que lo decide la linea: se arrastra el token de detras solo si no
    parece otro flag. Es una heuristica, y por eso esta escrita aqui: un valor
    que empiece por guion (un numero negativo) se quedaria huerfano. Los flags
    que el laboratorio toca de verdad estan en `_FLAGS` y no pasan por aqui.
    """
    fuera, i = [], 0
    while i < len(args):
        t = args[i]
        if t.split("=", 1)[0] != flag:
            fuera.append(t)
            i += 1
            continue
        if "=" in t:
            i += 1
            continue
        i += 2 if (i + 1 < len(args) and not args[i + 1].startswith("-")) else 1
    return fuera


def quita_flags(args: list[str], flags) -> list[str]:
    """Copia de `args` sin esos flags (con su valor si lo tienen).

    Se resuelve por la tabla `_FLAGS` cuando el flag esta en ella, de modo que
    quitar `-kvu` quita tambien `--kv-unified`: si solo se borrara el alias
    escrito, una unidad que use la forma larga se quedaria con el flag puesto y
    el brazo mediria otra configuracion sin avisar.
    """
    fuera = list(args)
    for f in flags:
        nombres, tipo = _alias_de(f)
        if nombres is None:
            fuera = _quita_desconocido(fuera, f)
        else:
            fuera = _quita(fuera, nombres, tipo == "valor")
    return fuera


def con_cambios(args: list[str], **kv) -> list[str]:
    """Copia de `args` con los flags pedidos sustituidos, anadidos o quitados.

    Reglas, y son las tres que hacen falta para que un A/B no mienta:
      - un flag con valor se SUSTITUYE este o no (no hay rama "si existe" y
        rama "si no existe", que es como H-031 acabo con dos caminos y ninguna
        comprobacion de que el cambio se hubiera aplicado);
      - `None` lo QUITA, en cualquiera de sus alias y en la forma `--flag=x`;
      - una bandera (`kvu`) se pone con True y se quita con False.

    Dos claves no son flags sino operaciones sobre la linea, y se aplican
    SIEMPRE en este orden, pase el que pase el llamante:

      `quitar=[...]`     flags a eliminar, con su valor si lo tienen. Va
                         primero para que un `con_cambios(a, quitar=["-md"],
                         draft_model=x)` no se borre a si mismo.
      `extra_args=[...]` tokens que se anaden TAL CUAL al final. Es la valvula
                         para un flag que la tabla todavia no conoce; lo que se
                         mide con el queda en el log de arranque del banco.

    Una clave desconocida es un error: `con_cambios(args, cache_rma=0)` tiene
    que cantar, no devolver la linea intacta y medir la configuracion vieja.
    """
    kv = dict(kv)
    quitar = kv.pop("quitar", None)
    extra = kv.pop("extra_args", None)
    fuera = list(args)
    if quitar is not None:
        if isinstance(quitar, str):
            raise ValueError("quitar espera una lista de flags, no una cadena")
        fuera = quita_flags(fuera, quitar)
    for clave, valor in kv.items():
        spec = _FLAGS.get(clave)
        if spec is None:
            raise ValueError(
                f"con_cambios no sabe tocar {clave!r}; conocidas: "
                f"{', '.join(sorted(_FLAGS))}, quitar, extra_args")
        nombres, tipo = spec
        fuera = _quita(fuera, nombres, tipo == "valor")
        if tipo == "bandera":
            if valor:
                fuera.append(nombres[0])
        elif valor is not None:
            fuera += [nombres[0], str(valor)]
    if extra is not None:
        if isinstance(extra, str):
            raise ValueError("extra_args espera una lista de tokens, no una cadena")
        fuera += [str(t) for t in extra]
    return fuera


def valor_de(args: list[str], clave: str):
    """Valor actual de un flag en la linea, o None. Para dejarlo por escrito
    en el informe sin volver a parsear la unidad."""
    nombres, tipo = _FLAGS[clave]
    for i, t in enumerate(args):
        base, _, pegado = t.partition("=")
        if base not in nombres:
            continue
        if tipo == "bandera":
            return True
        return pegado if pegado else (args[i + 1] if i + 1 < len(args) else None)
    return False if tipo == "bandera" else None


_RE_CACHE_RAM = re.compile(r"(--cache-ram[=\s]+)(\d+)")


def pon_cache_ram(texto_unidad: str, valor: int) -> str:
    """Deja `--cache-ram <valor>` en el texto de la unidad.

    Si el flag no esta, es un ERROR y no un cambio de cero: una fase que cree
    haber bajado la cache y no la ha bajado deja produccion con la
    configuracion que acaba de descartar.
    """
    nuevo, n = _RE_CACHE_RAM.subn(lambda m: f"{m.group(1)}{valor}", texto_unidad)
    if n == 0:
        raise RuntimeError(
            "la unidad no trae --cache-ram: el cambio no se habria aplicado y "
            "produccion se quedaria con la configuracion no elegida")
    return nuevo


_RE_LINEA_SPEC = re.compile(
    r"[ \t]*(?:--spec-type|-md|--model-draft|--spec-draft-n-max|--spec-draft-p-min)"
    r"[= \t]+\S+[ \t]*\\?\n")


def pon_mtp(texto_unidad: str, cabeza: str, n_max: int, p_min: float = 0.0) -> str:
    """Deja la cabeza MTP sidecar en el texto de la unidad (H-035).

    Inserta `--spec-type draft-mtp`, `-md <cabeza>`, `--spec-draft-n-max` y
    `--spec-draft-p-min` justo despues de la linea `--mmproj` (la vision es
    requisito y asi quedan juntas las dos piezas que dependen del modelo). Si
    la unidad ya trae flags de especulacion se sustituyen, no se duplican: dos
    `--spec-type` en una linea de arranque es exactamente el tipo de error que
    un `daemon-reload` no avisa y un `restart` convierte en produccion caida.

    Sin `--mmproj` es ERROR: no hay ancla y la unidad no es la que se probo.
    """
    if not os.path.isabs(cabeza) or not cabeza.endswith(".gguf"):
        raise RuntimeError(f"cabeza MTP no es una ruta absoluta a un .gguf: {cabeza!r}")
    if not isinstance(n_max, int) or isinstance(n_max, bool) or n_max < 1:
        raise RuntimeError(f"n_max invalido: {n_max!r}")
    limpio = _RE_LINEA_SPEC.sub("", texto_unidad)
    ancla = re.search(r"^([ \t]*)--mmproj[ \t=]+\S+[ \t]*\\\n", limpio, re.M)
    if not ancla:
        raise RuntimeError(
            "la unidad no trae --mmproj en su propia linea: sin ese ancla no se "
            "inserta la cabeza MTP (la vision es requisito, no se despliega sin ella)")
    sangria = ancla.group(1)
    bloque = (f"{sangria}--spec-type draft-mtp \\\n"
              f"{sangria}-md {cabeza} \\\n"
              f"{sangria}--spec-draft-n-max {n_max} \\\n"
              f"{sangria}--spec-draft-p-min {p_min:g} \\\n")
    return limpio[:ancla.end()] + bloque + limpio[ancla.end():]


def quita_mtp(texto_unidad: str) -> str:
    """Deja la unidad sin cabeza MTP (el `aplicar` cuando H-035 NO adopta)."""
    return _RE_LINEA_SPEC.sub("", texto_unidad)


_RE_MODEL = re.compile(r"(^[ \t]*(?:--model|-m)[ \t=]+)(\S+)", re.M)


def pon_modelo(texto_unidad: str, gguf: str) -> str:
    """Cambia el GGUF del tronco en la unidad (H-036 requant). Sin `--model` es
    error: la unidad no es la que se probo."""
    if not os.path.isabs(gguf) or not gguf.endswith(".gguf"):
        raise RuntimeError(f"modelo no es una ruta absoluta a un .gguf: {gguf!r}")
    nuevo, n = _RE_MODEL.subn(lambda m: f"{m.group(1)}{gguf}", texto_unidad)
    if n != 1:
        raise RuntimeError(f"la unidad trae {n} lineas --model; se esperaba exactamente 1")
    return nuevo


# ================================================================== peticiones
def peticion_chat(url: str, clave: str | None, messages: list[dict],
                  timeout: float = 900, **params) -> dict:
    """Una peticion a /v1/chat/completions. Devuelve el cuerpo ya validado.

    Todo lo que no sea un 200 con cuerpo JSON parseable es
    ErrorInfraestructura: el banco no ha podido medir, y eso no es ni una
    medida de cero ni un fallo del modelo (metodologia, regla 6).

    `messages` viaja TAL CUAL: un `content` que sea una lista de partes
    (`{"type": "text"...}` + `{"type": "image_url"...}`) es contenido
    multimodal valido y aqui no se toca ni se serializa a texto. La fase de
    vision de H-034 depende de eso.

    Una conexion que se corta a media respuesta -- que es lo que hace un
    servidor al que la GPU se le lleva por delante (#27306) -- entra tambien
    por aqui como ErrorInfraestructura. Antes se escapaba como
    `http.client.RemoteDisconnected` cruda porque no es `URLError`, y la fase
    que la recibia moria por una excepcion sin clasificar en vez de anotar el
    techo de contexto.
    """
    cuerpo = dict(params)
    cuerpo["messages"] = messages
    datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
    cab = {"Content-Type": "application/json"}
    if clave:
        cab["Authorization"] = f"Bearer {clave}"
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=datos, headers=cab)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                raise ErrorInfraestructura(f"HTTP {r.status} en /v1/chat/completions")
            bruto = r.read()
    except urllib.error.HTTPError as e:
        raise ErrorInfraestructura(f"HTTP {e.code} en /v1/chat/completions") from e
    except urllib.error.URLError as e:
        raise ErrorInfraestructura(f"sin respuesta del servidor: {e.reason}") from e
    except TimeoutError as e:
        raise ErrorInfraestructura(f"timeout tras {timeout} s") from e
    except (http.client.HTTPException, OSError) as e:
        raise ErrorInfraestructura(
            f"la conexion con el servidor se corto a media respuesta "
            f"({type(e).__name__}: {e}): el proceso ha muerto o la GPU se lo "
            "ha llevado por delante") from e
    d = cuerpo_json(bruto)
    # Reloj de pared del cliente: no sustituye a timings, pero delata una
    # respuesta cacheada por un proxy o una cola que no se ve en el servidor.
    d["wall_ms"] = round((time.time() - t0) * 1000, 1)
    return d


def ttft_ms(d: dict) -> float:
    """TTFT = `timings.prompt_ms`: lo que tarda el prefill en estar listo.

    Su AUSENCIA no es cero, igual que en `validacion.timings`: es una medida
    que no se ha tomado, y un TTFT de 0 ms en una tabla es folclore.
    """
    t = timings(d)
    v = t.get("prompt_ms")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ErrorInfraestructura(
            "la respuesta no trae 'timings.prompt_ms': el TTFT no es medible")
    v = float(v)
    if not math.isfinite(v) or v < 0:
        raise ErrorInfraestructura(f"timings.prompt_ms={v!r} no es un tiempo plausible")
    return v


def cache_n(d: dict) -> int:
    """Tokens servidos desde la cache de prompt. 0 es un valor legitimo aqui
    (cache vacia), asi que se admite; lo que no se admite es inventarlo."""
    t = timings(d)
    v = t.get("cache_n", 0)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v)


def mediana(valores) -> float | None:
    xs = sorted(float(v) for v in valores if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def clave_de(ctx) -> str:
    """La credencial de la campana, por la unica puerta que `Contexto` ofrece.

    `Contexto` no la expone como atributo publico a proposito (para que no
    acabe en un `repr(ctx)` dentro de un traceback); la ofrece dentro del
    entorno del proceso hijo. Las fases necesitan el valor para autenticarse
    por HTTP, asi que el acceso queda en UN sitio y con su motivo escrito.
    """
    return ctx.entorno_con_clave()["LLAMA_API_KEY"]


# =================================================================== el proceso
def soporta_env_api_key(binario: str, ejecutar=None) -> bool:
    """¿Acepta este binario la clave por entorno (`LLAMA_ARG_API_KEY`)?

    Se pregunta a la AYUDA del binario en vez de suponerlo por la version: el
    banco puede estar midiendo una build de hace seis meses o un fork. Si no
    se puede preguntar, se responde que no, que es el camino conservador (un
    fichero temporal 600 funciona siempre).
    """
    ejecutar = ejecutar or (lambda cmd: subprocess.run(
        cmd, capture_output=True, text=True, timeout=60))
    try:
        p = ejecutar([binario, "--help"])
    except Exception:
        return False
    return "LLAMA_ARG_API_KEY" in ((p.stdout or "") + (p.stderr or ""))


def puerto_ocupado(puerto: int, timeout: float = 0.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", puerto))
        return True
    except OSError:
        return False
    finally:
        s.close()


def espera_puerto_libre(puerto: int, limite: float = 30, pausa: float = 0.5,
                        traza=print) -> bool:
    """Que el proceso haya muerto no es que el puerto este libre.

    Si el siguiente brazo arranca contra un socket todavia ocupado, o falla al
    bindear o -- peor -- mide contra el servidor anterior.
    """
    t0 = time.time()
    while time.time() - t0 < limite:
        if not puerto_ocupado(puerto):
            return True
        time.sleep(pausa)
    traza(f"    [!] el puerto {puerto} sigue ocupado {limite} s despues de matar el banco")
    return False


def rss_mb(pid: int) -> float | None:
    """VmRSS del proceso en MiB, leido de /proc. None si no se puede leer."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return round(int(linea.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


# ============================================================ vigilancia del kernel
def marca_ahora() -> str:
    """Instante actual en el formato que entiende `journalctl --since`.

    Se toma ANTES de empezar la fase y se pasa a `dmesg_desde`: asi lo que se
    lee del kernel es lo que ha pasado durante la medida, no el historial del
    arranque de la maquina.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def dmesg_desde(marca: str, ejecutar=None) -> list[str] | None:
    """Lineas del kernel con `amdgpu` desde `marca`, o None si no se puede mirar.

    Dos intentos, en este orden: `journalctl -k` a secas y, si no sale, con
    `sudo -n`. Leer el anillo del kernel no siempre esta al alcance del usuario
    del laboratorio, y el matiz importa: **None NO es "sano"**. Un reset de GPU
    que no se ha podido mirar es una escalera de contexto sin vigilancia, y la
    fase tiene que escribirlo asi en el informe (#27306: el driver hace un
    `llama_decode(ctx_dft)` tras cada ubatch y ya nos costo un DeviceLost real
    en H-014). Una lista vacia si es "no ha pasado nada"; None es "no lo se".

    El filtro es `amdgpu` a secas. Decidir QUE linea es un incidente es cosa de
    quien vigila -- la fase de H-034 lo hace con su propio patron --, porque un
    filtro estrecho aqui escondería una linea nueva que todavia no sabemos leer.
    """
    ejecutar = ejecutar or (lambda cmd: subprocess.run(
        cmd, capture_output=True, text=True, timeout=60))
    jctl = os.environ.get("JOURNALCTL", JOURNALCTL)
    sudo = os.environ.get("SUDO", SUDO)
    base = [jctl, "-k", "--no-pager", "--since", marca]
    intentos = [base]
    if sudo.strip():
        intentos.append(shlex.split(sudo) + base)
    for cmd in intentos:
        try:
            p = ejecutar(cmd)
        except Exception:
            continue
        if getattr(p, "returncode", 1) != 0:
            continue
        return [l for l in (p.stdout or "").splitlines() if "amdgpu" in l.lower()]
    return None


class ServidorBanco:
    """Un llama-server de laboratorio en `puerto`, con la vida atada al `with`.

    Al entrar: sustituye `--host`/`--port`, coloca la credencial fuera de argv,
    lanza el proceso en su propia sesion y espera a que sirva el modelo. Al
    salir -- pase lo que pase, incluida una excepcion dentro del `with` o un
    fallo de la propia espera -- mata el grupo de procesos, borra el fichero
    temporal de clave y espera a que el puerto quede libre.
    """

    def __init__(self, binario: str, args: list[str], puerto: int,
                 modelo: str | None = None, clave: str | None = None,
                 entorno: dict | None = None, limite: float | None = None,
                 pausa: float | None = None, gracia: float | None = None,
                 log=print, dir_log: str | None = None, etiqueta: str = "banco"):
        self.binario = binario
        self.args = list(args)
        self.puerto = int(puerto)
        self.modelo = modelo
        self.clave = clave
        self.entorno = entorno
        self.limite = float(os.environ.get("BANCO_LIMITE_ARRANQUE", LIMITE_ARRANQUE)
                            if limite is None else limite)
        self.pausa = float(os.environ.get("BANCO_PAUSA", PAUSA_ESPERA)
                           if pausa is None else pausa)
        self.gracia = float(os.environ.get("BANCO_GRACIA", GRACIA_CIERRE)
                            if gracia is None else gracia)
        self.log = log
        self.dir_log = dir_log
        self.etiqueta = etiqueta
        self.proc = None
        self.args_finales: list[str] = []
        self.origen_clave = None
        # Rastro para despues del cierre: `proc` y `_fichero_clave` se vacian
        # al matar el servidor, y sin esto no hay forma de comprobar desde
        # fuera que el proceso murio y que el fichero de clave se borro.
        self.ultimo_pid = None
        self.ultimo_fichero_clave = None
        self._fichero_clave = None
        self._flog = None
        # El log del hijo es una MEDIDA mas: H-034 confirma que la especulacion
        # esta activa leyendo `draft acceptance = ...` del propio servidor. Por
        # eso se guarda siempre la ruta y el desplazamiento donde empieza ESTE
        # arranque (el fichero se abre en modo anadir y puede traer la cola de
        # una ejecucion anterior con la misma etiqueta).
        self.ruta_log = None
        self._log_temporal = False
        self._offset_log = 0

    # -- ciclo de vida ----------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.puerto}"

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def rss_mb(self) -> float | None:
        return rss_mb(self.pid) if self.pid else None

    def esta_vivo(self) -> bool:
        """¿Sigue en pie el proceso? Sondeo suelto, sin esperas ni reintentos."""
        return self.proc is not None and self.proc.poll() is None

    def log_tail(self, n: int = 50) -> list[str]:
        """Ultimas `n` lineas que ha escrito ESTE arranque, sin la cola previa.

        Devuelve lista vacia si todavia no hay nada escrito. No lanza: quien la
        llama esta buscando una linea concreta en el log de un servidor que
        quiza acaba de morirse, y una excepcion aqui taparia el motivo real.
        """
        if not self.ruta_log:
            return []
        try:
            if self._flog:
                self._flog.flush()
            with open(self.ruta_log, encoding="utf-8", errors="replace") as f:
                f.seek(self._offset_log)
                lineas = f.read().splitlines()
        except OSError:
            return []
        return lineas[-n:] if n > 0 else lineas

    def _abre_log(self) -> None:
        """Abre el fichero al que va el stdout/stderr del hijo.

        Sin `dir_log` se usa un temporal en vez de /dev/null: tirar la salida
        del servidor deja `log_tail` sin nada que leer, y con ella se pierde la
        unica prueba de que la especulacion estaba activa. El temporal se borra
        en `cierra()`.
        """
        if self.dir_log:
            os.makedirs(self.dir_log, exist_ok=True)
            self.ruta_log = os.path.join(self.dir_log, f"banco-{self.etiqueta}.log")
            self._flog = open(self.ruta_log, "a", encoding="utf-8", errors="replace")
        else:
            fd, ruta = tempfile.mkstemp(prefix=f"banco-{self.etiqueta}-", suffix=".log")
            self.ruta_log, self._log_temporal = ruta, True
            self._flog = os.fdopen(fd, "a", encoding="utf-8", errors="replace")
        # La linea completa va SOLO aqui, no al log de la campana ni al JSON:
        # ahi no pinta nada y solo aumenta la superficie de fuga.
        self._flog.write("\n=== " + shlex.join([self.binario, *self.args_finales]) + "\n")
        self._flog.flush()
        self._offset_log = self._flog.tell()

    def _prepara(self) -> tuple[list[str], dict]:
        finales = con_cambios(self.args, host="127.0.0.1", port=self.puerto)
        entorno = dict(self.entorno if self.entorno is not None else os.environ)
        entorno.pop("LLAMA_ARG_API_KEY", None)
        if not self.clave:
            return con_cambios(finales, api_key=None, api_key_file=None), entorno
        if soporta_env_api_key(self.binario):
            self.origen_clave = "LLAMA_ARG_API_KEY"
            entorno["LLAMA_ARG_API_KEY"] = self.clave
            return con_cambios(finales, api_key=None, api_key_file=None), entorno
        # Sin soporte por entorno, un fichero temporal SOLO legible por su
        # dueno. Nunca `--api-key <valor>`: eso la pone en /proc/<pid>/cmdline,
        # que es exactamente el agujero de H-021.
        fd, ruta = tempfile.mkstemp(prefix="banco-clave-", suffix=".txt")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.clave + "\n")
        self._fichero_clave = self.ultimo_fichero_clave = ruta
        self.origen_clave = "--api-key-file temporal 600"
        return con_cambios(finales, api_key=None, api_key_file=ruta), entorno

    def __enter__(self) -> "ServidorBanco":
        self.args_finales, entorno = self._prepara()
        self._abre_log()
        try:
            self.proc = subprocess.Popen(
                [self.binario, *self.args_finales], stdout=self._flog,
                stderr=subprocess.STDOUT, env=entorno, start_new_session=True)
        except OSError as e:
            self.cierra()
            raise ErrorInfraestructura(
                f"no pude lanzar el servidor de banco {self.binario}: {e}") from e
        self.ultimo_pid = self.proc.pid
        self.log(f"    banco[{self.etiqueta}] pid {self.proc.pid} en el puerto "
                 f"{self.puerto} (clave via {self.origen_clave or 'sin clave'})")
        try:
            sano = salud.espera_proceso(self.proc, self.puerto, modelo=self.modelo,
                                        clave=self.clave, limite=self.limite,
                                        pausa=self.pausa, traza=self.log)
        except BaseException:
            self.cierra()
            raise
        if not sano:
            self.cierra()
            raise ErrorInfraestructura(
                f"el servidor de banco [{self.etiqueta}] no llego a servir "
                f"{self.modelo!r} en el puerto {self.puerto} en {self.limite} s")
        return self

    def __exit__(self, *exc) -> bool:
        self.cierra()
        return False

    def cierra(self) -> None:
        """SIGTERM al grupo, SIGKILL a los `gracia` segundos, puerto libre.

        Idempotente: se llama desde `__exit__` y tambien desde los caminos de
        fallo de `__enter__`, y llamarla dos veces no puede romper nada.
        """
        proc, self.proc = self.proc, None
        try:
            if proc is not None and proc.poll() is None:
                self._senal(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=self.gracia)
                except subprocess.TimeoutExpired:
                    self.log(f"    banco[{self.etiqueta}] no murio con SIGTERM "
                             f"en {self.gracia} s: SIGKILL")
                    self._senal(proc, signal.SIGKILL)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.log(f"    [!] banco[{self.etiqueta}] sobrevivio al SIGKILL")
            elif proc is not None:
                proc.wait(timeout=5)
        finally:
            if self._fichero_clave:
                try:
                    os.unlink(self._fichero_clave)
                except OSError:
                    pass
                self._fichero_clave = None
            if self._flog:
                try:
                    self._flog.close()
                except OSError:
                    pass
                self._flog = None
            if self._log_temporal and self.ruta_log:
                try:
                    os.unlink(self.ruta_log)
                except OSError:
                    pass
                self._log_temporal = False
            if proc is not None:
                espera_puerto_libre(self.puerto, limite=self.gracia + 10,
                                    traza=self.log)

    @staticmethod
    def _senal(proc, sig) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass
