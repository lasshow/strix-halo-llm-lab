#!/usr/bin/env python3
"""Runner generico de campanas A/B sobre una unidad de llama-server.

Sustituye a `campana-nocturna.py`, que era un script de un solo uso: worktree
reutilizado sin comprobar a que apuntaba, nombre de backup `bak-campana-
20260910` cableado, baseline `311d421` escrito a mano en el informe, produccion
arrancada SIEMPRE al final aunque hubiera empezado parada, y sin una sola
prueba. Se repara lo que la auditoria de H-031 encontro:

  A. La credencial de los gates sale del ExecStart efectivo (credencial.sh),
     nunca de un fichero secundario, y viaja por el ENTORNO del proceso hijo:
     no se imprime, no entra en el log ni en el JSON de resultados.
  B. La espera de servicio es la compartida (salud.espera_servicio): unidad
     activa Y /health 200 Y modelo correcto. Ya no basta con que algo responda
     en el puerto.
  C. Los gates son los dos scripts reales, ejecutados COMO PROCESOS y no
     reimplementados aqui: `restauracion.sh` (generico) y `smoke-test.sh`
     (exacto, 17x23 == "391"). El gate de la campana vieja se conformaba con
     "contenido no vacio y finish_reason=stop", que aprueba un backend que
     responde mal.
  D. La build candidata se compila en su propio directorio (`builds.sh`) y se
     promueve por symlink. El rollback deshace las DOS cosas: la unidad desde
     su backup y la build con `builds.sh volver`. Compilar encima del arbol
     productivo hacia que "restaurar la unidad" no restaurara nada.
  E. Todo lo que era constante es ahora parametro, el estado inicial de
     produccion se registra y SE RESPETA, y hay pruebas (tests/test_p0.py).

Regla dura: **sin produccion en marcha no hay gate, y sin gate no se aplica
nada.** Si la unidad estaba parada al empezar, la campana mide, decide y
escribe la recomendacion, pero no toca la unidad ni promueve la build, y la
deja parada como estaba.

Fases enchufables
-----------------
El nucleo no sabe que se mide. Cada fase es una funcion `fase(ctx) -> dict`
cargada con `--fase fichero.py:funcion`, y H-032/H-033 se escriben como
modulos sueltos sin tocar este fichero. Claves reconocidas del dict:

    nombre    etiqueta para el informe (por defecto, el nombre de la funcion)
    resumen   dict libre, va tal cual al JSON de resultados
    adoptar   bool: esta fase vota adoptar el candidato
    aplicar   callable(texto_unidad) -> texto_unidad, cambio en la unidad
    error     str: la fase no pudo ejecutarse (no aborta las demas)

TABLA DE SALIDAS
    0  todo bien (o campana en seco: produccion estaba parada y se respeto)
    1  error de uso o de entorno
    2  alguna fase fallo
    3  GATE ROJO: se revirtio unidad y build, y produccion quedo sana
    4  GATE ROJO Y ROLLBACK FALLIDO: produccion no volvio a estado sano

Uso:
    python3 campana.py --run-id h032-cacheram \\
        --baseline-sha df03399... --candidato-sha abc1234... \\
        --unidad llama-flashnext --modelo qwen3.8-flash-next \\
        --fase fases_h032.py:correccion_cache_ram
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

import prompts as mod_prompts  # noqa: E402
import salud  # noqa: E402
from validacion import ErrorInfraestructura  # noqa: E402

SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")
SUDO = os.environ.get("SUDO", "sudo -n")
DIR_UNIDADES = os.environ.get("LLAMA_UNIT_DIR", "/etc/systemd/system")

SALIDA_OK, SALIDA_ENTORNO, SALIDA_FASE = 0, 1, 2
SALIDA_GATE, SALIDA_ROLLBACK = 3, 4


def sh(cmd: str, check: bool = False, timeout: float | None = None) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} -> {r.returncode}: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def _sudo(cmd: str) -> str:
    """Antepone sudo solo si hay sudo configurado (las pruebas lo vacian)."""
    return f"{SUDO} {cmd}".strip()


class Contexto:
    """Lo que una fase necesita del entorno, y nada mas.

    No expone la clave como atributo publico por costumbre: se pide con
    `entorno_con_clave()`, que devuelve un dict de entorno para un proceso
    hijo. Asi la clave no acaba en un `repr(ctx)` dentro de un traceback.
    """

    def __init__(self, args, builds: dict, clave: str, registro):
        self.args = args
        self.unidad = args.unidad
        self.modelo = args.modelo
        self.puerto_prod = args.puerto_prod
        self.puerto_banco = args.puerto_banco
        self.builds = builds              # {"baseline": ruta, "candidato": ruta}
        self.dir_salida = args.salida
        self._clave = clave
        self._registro = registro
        self.notas: list[str] = []

    # -- utilidades -------------------------------------------------------
    def log(self, msg: str) -> None:
        linea = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        print(linea, flush=True)
        with open(os.path.join(self.dir_salida, "campana.log"), "a",
                  encoding="utf-8") as f:
            f.write(linea + "\n")

    def nota(self, msg: str) -> None:
        self.notas.append(msg)
        self.log("NOTA: " + msg)

    def medida(self, reg: dict) -> None:
        """Una linea por peticion en el JSONL crudo, como el resto del banco."""
        reg = dict(reg, ts=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with open(self._registro, "a", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")

    def entorno_con_clave(self) -> dict:
        return dict(os.environ, LLAMA_API_KEY=self._clave)

    def binario(self, cual: str) -> str:
        return os.path.join(self.builds[cual], "build", "bin", "llama-server")

    def espera(self, unidad: str, puerto: int, limite: float = 900) -> bool:
        return salud.espera_servicio(unidad, puerto, modelo=self.modelo,
                                     limite=limite, clave=self._clave,
                                     traza=self.log)

    # -- corpus -----------------------------------------------------------
    def corpus(self, objetivo: int) -> dict:
        """Corpus congelado de ese tamano: EL MISMO fichero para todos los brazos."""
        return mod_prompts.carga(objetivo, self.args.dir_corpus)

    def verifica_corpus(self, objetivo: int, prompt_n: int) -> int:
        """Aborta si el `prompt_n` real se sale de la tolerancia del corpus."""
        return mod_prompts.verifica_prompt_n(objetivo, prompt_n,
                                             self.args.tolerancia_corpus)


def id_build(sha: str, patch: str | None) -> str:
    """Mismo identificador que builds.sh: sha, o sha+8 hex del sha256 del parche."""
    if not patch:
        return sha
    import hashlib
    with open(patch, "rb") as f:
        return f"{sha}+{hashlib.sha256(f.read()).hexdigest()[:8]}"


# ------------------------------------------------------------------ unidad
def ruta_unidad(unidad: str) -> str:
    return os.path.join(DIR_UNIDADES, f"{unidad}.service")


def lee_unidad(unidad: str) -> str:
    ruta = ruta_unidad(unidad)
    txt = sh(f"cat {shlex.quote(ruta)}")
    if not txt:
        txt = sh(_sudo(f"cat {shlex.quote(ruta)}"))
    if not txt:
        raise RuntimeError(f"no pude leer {ruta}")
    return txt


def escribe_unidad(unidad: str, texto: str, tmp_dir: str) -> None:
    tmp = os.path.join(tmp_dir, f"{unidad}.service.nuevo")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    sh(_sudo(f"cp {shlex.quote(tmp)} {shlex.quote(ruta_unidad(unidad))}"), check=True)
    sh(_sudo(f"{SYSTEMCTL} daemon-reload"))


def aplica_cambios_texto(texto: str, cambios: list[str]) -> tuple[str, list[str]]:
    """`PATRON=>REEMPLAZO` sobre el texto de la unidad.

    Un patron que no casa es un ERROR, no un cambio de cero: la campana vieja
    tenia una rama que anadia `--cache-ram` "si no estaba" y otra que lo
    sustituia, y ninguna comprobaba despues que el texto hubiera cambiado.
    """
    hechos = []
    for c in cambios:
        if "=>" not in c:
            raise RuntimeError(f"--cambio-unidad mal formado (falta '=>'): {c!r}")
        patron, reemplazo = c.split("=>", 1)
        nuevo, n = re.subn(patron, reemplazo, texto)
        if n == 0:
            raise RuntimeError(
                f"el patron {patron!r} no aparece en la unidad: el cambio no se "
                "habria aplicado y la campana mediria la configuracion vieja")
        texto = nuevo
        hechos.append(f"{patron} -> {reemplazo} ({n})")
    return texto, hechos


# ------------------------------------------------------------------- gates
def gate_restauracion(ctx: Contexto) -> tuple[bool, str]:
    p = subprocess.run(
        ["bash", os.path.join(AQUI, "restauracion.sh"), ctx.unidad,
         str(ctx.puerto_prod), ctx.modelo],
        capture_output=True, text=True, timeout=600, env=ctx.entorno_con_clave())
    return p.returncode == 0, p.stdout + p.stderr


def gate_smoke_exacto(ctx: Contexto) -> tuple[bool, str]:
    p = subprocess.run(
        ["bash", os.path.join(AQUI, "smoke-test.sh"),
         f"http://127.0.0.1:{ctx.puerto_prod}"],
        capture_output=True, text=True, timeout=900, env=ctx.entorno_con_clave())
    return p.returncode == 0, p.stdout + p.stderr


def pasa_los_gates(ctx: Contexto, resultados: dict, etiqueta: str) -> bool:
    """Los DOS gates, en orden, sin cortocircuito: interesa ver los dos informes."""
    veredictos = {}
    for nombre, fn in (("restauracion", gate_restauracion),
                       ("smoke_exacto", gate_smoke_exacto)):
        try:
            ok, salida = fn(ctx)
        except Exception as e:                     # subproceso que no arranca
            ok, salida = False, f"{type(e).__name__}: {e}"
        veredictos[nombre] = ok
        ctx.log(f"gate[{etiqueta}] {nombre}: {'VERDE' if ok else 'ROJO'}")
        if not ok:
            for linea in salida.strip().splitlines()[-12:]:
                ctx.log(f"    | {linea}")
    resultados.setdefault("gates", {})[etiqueta] = veredictos
    return all(veredictos.values())


# ------------------------------------------------------------------- fases
def carga_fase(spec: str):
    """`fichero.py:funcion`. Se resuelve contra scripts/ si es relativo."""
    if ":" not in spec:
        raise RuntimeError(f"--fase mal formado, falta ':funcion': {spec!r}")
    ruta, funcion = spec.rsplit(":", 1)
    if not os.path.isabs(ruta):
        candidata = os.path.join(AQUI, ruta)
        ruta = candidata if os.path.exists(candidata) else ruta
    if not os.path.exists(ruta):
        raise RuntimeError(f"no encuentro el modulo de fase {ruta}")
    nombre = "fase_" + os.path.basename(ruta).replace(".py", "").replace("-", "_")
    esp = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(esp)
    esp.loader.exec_module(mod)
    if not hasattr(mod, funcion):
        raise RuntimeError(f"{ruta} no define {funcion}()")
    return getattr(mod, funcion)


# -------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True,
                    help="identifica la campana; nombra el backup .bak-<run-id>")
    ap.add_argument("--unidad", default="llama-flashnext")
    ap.add_argument("--modelo", default="qwen3.8-flash-next")
    ap.add_argument("--baseline-sha", default=None)
    ap.add_argument("--candidato-sha", default=None)
    ap.add_argument("--repo", default="https://github.com/ggml-org/llama.cpp",
                    help="origen para builds.sh construir")
    ap.add_argument("--patch", default=None, help="parche a aplicar al candidato")
    ap.add_argument("--puerto-prod", type=int, default=8080)
    ap.add_argument("--puerto-banco", type=int, default=8081)
    ap.add_argument("--fase", action="append", default=[],
                    help="fichero.py:funcion (repetible, en orden)")
    ap.add_argument("--cambio-unidad", action="append", default=[],
                    help="PATRON=>REEMPLAZO sobre el texto de la unidad")
    ap.add_argument("--forzar-adopcion", action="store_true",
                    help="promueve el candidato sin voto de las fases (ensayo "
                         "del mecanismo de gates y rollback)")
    ap.add_argument("--saltar-construccion", action="store_true",
                    help="las builds ya estan en LLAMA_BUILDS_DIR y registradas")
    ap.add_argument("--dir-corpus", default=mod_prompts.DIR_CORPUS)
    ap.add_argument("--tolerancia-corpus", type=int, default=mod_prompts.TOLERANCIA)
    ap.add_argument("--salida", default=None,
                    help="directorio de log/JSONL/resultados (por defecto "
                         "evidencias/<run-id>)")
    a = ap.parse_args(argv)

    a.salida = a.salida or os.path.join(os.path.dirname(AQUI), "evidencias", a.run_id)
    os.makedirs(a.salida, exist_ok=True)
    registro = os.path.join(a.salida, "medidas.jsonl")

    # --- credencial: del ExecStart efectivo, nunca de un .env (fallo A) ---
    p = subprocess.run(["bash", os.path.join(AQUI, "credencial.sh"), "--crudo",
                        a.unidad], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        print(f"No pude derivar la credencial de {a.unidad}: "
              f"{p.stderr.strip()[:200]}", file=sys.stderr)
        return SALIDA_ENTORNO
    clave = p.stdout.strip()

    try:
        fases = [(s, carga_fase(s)) for s in a.fase]
    except RuntimeError as e:
        print(f"Fase ilegible: {e}", file=sys.stderr)
        return SALIDA_ENTORNO

    resultados = {
        "run_id": a.run_id, "unidad": a.unidad, "modelo": a.modelo,
        "baseline_sha": a.baseline_sha, "candidato_sha": a.candidato_sha,
        "patch": a.patch,
        "inicio": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ctx = Contexto(a, {}, clave, registro)
    ctx.log("=" * 70)
    ctx.log(f"CAMPANA {a.run_id}: inicio")

    # --- estado inicial: se registra ANTES de tocar nada y manda al final ---
    estado_inicial = sh(f"{SYSTEMCTL} is-active {a.unidad}")
    resultados["estado_inicial"] = estado_inicial
    ctx.log(f"estado inicial de {a.unidad}: {estado_inicial!r}")

    ruta = ruta_unidad(a.unidad)
    backup = f"{ruta}.bak-{a.run_id}"
    try:
        unidad_original = lee_unidad(a.unidad)
        sh(_sudo(f"cp {shlex.quote(ruta)} {shlex.quote(backup)}"), check=True)
    except RuntimeError as e:
        print(f"No pude respaldar la unidad: {e}", file=sys.stderr)
        return SALIDA_ENTORNO
    resultados["backup_unidad"] = backup
    ctx.log(f"backup de la unidad en {backup}")

    # --- builds: cada una en su directorio, nunca sobre el arbol productivo --
    if not a.saltar_construccion:
        for cual, sha_ in (("baseline", a.baseline_sha), ("candidato", a.candidato_sha)):
            if not sha_:
                continue
            extra = f" --patch {shlex.quote(a.patch)}" if (a.patch and cual == "candidato") else ""
            ctx.log(f"construyo {cual} {sha_[:12]}")
            try:
                sh(f"bash {shlex.quote(os.path.join(AQUI, 'builds.sh'))} construir "
                   f"{shlex.quote(a.repo)} {shlex.quote(sha_)}{extra}",
                   check=True, timeout=7200)
            except Exception as e:
                print(f"Fallo construyendo {cual}: {e}", file=sys.stderr)
                return SALIDA_ENTORNO
    dir_builds = os.environ.get("LLAMA_BUILDS_DIR", "/models/llama-builds")
    # El identificador de build de builds.sh es <sha> o <sha>+<sha256(parche)[:8]>
    # cuando el candidato lleva parche: dos builds del mismo sha con y sin
    # parche NO comparten directorio.
    ids = {"baseline": a.baseline_sha,
           "candidato": id_build(a.candidato_sha, a.patch) if a.candidato_sha else None}
    for cual, id_ in ids.items():
        if id_:
            ctx.builds[cual] = os.path.join(dir_builds, id_)

    # ------------------------------------------------------------- fases --
    parada_por_nosotros = False
    votos, aplicadores, fallos_fase = [], [], 0

    # SIGTERM/SIGINT/SIGHUP se convierten en excepcion para que se ejecuten los
    # `finally` (matar el servidor de banco, rearrancar produccion). Sin esto,
    # al abortar la cadena el 11-09 el runner y su banco de 87 GB siguieron
    # vivos con produccion parada.
    class Interrumpida(Exception):
        pass

    def _senal(num, _marco):
        raise Interrumpida(f"senal {num}")

    for sig_ in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig_, _senal)

    try:
        if estado_inicial == "active":
            ctx.log(f"paro {a.unidad} (la GPU no da para dos servidores)")
            sh(_sudo(f"{SYSTEMCTL} stop {a.unidad}"))
            parada_por_nosotros = True
            time.sleep(2)

        for spec, fn in fases:
            ctx.log(f"--- fase {spec}")
            try:
                r = fn(ctx) or {}
            except Interrumpida as e:
                fallos_fase += 1
                ctx.nota(f"campana interrumpida ({e}) en la fase {spec}: paro aqui")
                resultados.setdefault("fases", {})[spec] = {"error": f"interrumpida: {e}"}
                resultados["interrumpida"] = str(e)
                break
            except Exception as e:
                fallos_fase += 1
                ctx.nota(f"fase {spec} fallo: {type(e).__name__}: {e}")
                resultados.setdefault("fases", {})[spec] = {"error": f"{type(e).__name__}: {e}"}
                continue
            nombre = r.get("nombre") or getattr(fn, "__name__", spec)
            resultados.setdefault("fases", {})[nombre] = r.get("resumen", {})
            if r.get("error"):
                fallos_fase += 1
                ctx.nota(f"fase {nombre}: {r['error']}")
            if "adoptar" in r:
                votos.append(bool(r["adoptar"]))
            if callable(r.get("aplicar")):
                aplicadores.append((nombre, r["aplicar"]))
    except Interrumpida as e:
        # senal fuera de una fase (p. ej. durante la parada de produccion)
        fallos_fase += 1
        ctx.nota(f"campana interrumpida ({e}) fuera de fase: paro aqui")
        resultados["interrumpida"] = str(e)
    finally:
        if parada_por_nosotros:
            ctx.log(f"arranco {a.unidad} (estaba activa al empezar)")
            sh(_sudo(f"{SYSTEMCTL} start {a.unidad}"))

    if resultados.get("interrumpida"):
        # Interrumpida: no se aplica nada, produccion ya se ha rearrancado.
        ctx.log("campana interrumpida: no se aplica ni promueve nada")
        return _cierra(ctx, resultados, SALIDA_FASE)

    adoptar = a.forzar_adopcion or (bool(votos) and all(votos))
    resultados["votos_fases"] = votos
    resultados["adoptar"] = adoptar

    # Sin produccion en marcha no hay gate, y sin gate no se aplica nada.
    if estado_inicial != "active":
        ctx.log("produccion estaba parada al empezar: NO se aplica nada y se "
                "deja parada. La decision queda escrita en resultados.json.")
        resultados["aplicado"] = False
        resultados["motivo_no_aplicado"] = (
            "estado_inicial != active: sin gate posible no se toca la unidad "
            "ni se promueve la build")
        return _cierra(ctx, resultados,
                       SALIDA_FASE if fallos_fase else SALIDA_OK)

    try:
        if not ctx.espera(a.unidad, a.puerto_prod, limite=900):
            ctx.nota("produccion no volvio a estado sano tras las fases")
            return _cierra(ctx, resultados, SALIDA_ROLLBACK)
    except ErrorInfraestructura as e:
        ctx.nota(str(e))
        return _cierra(ctx, resultados, SALIDA_ROLLBACK)

    # ------------------------------------------- aplicar y promover ------
    cambios, promovido = [], False
    try:
        texto = unidad_original
        for nombre, fn in aplicadores:
            texto = fn(texto)
            cambios.append(f"fase {nombre}")
        texto, hechos = aplica_cambios_texto(texto, a.cambio_unidad)
        cambios += hechos
        if texto != unidad_original:
            escribe_unidad(a.unidad, texto, a.salida)
            sh(_sudo(f"{SYSTEMCTL} restart {a.unidad}"))
        if adoptar and a.candidato_sha:
            sh(f"bash {shlex.quote(os.path.join(AQUI, 'builds.sh'))} promover "
               f"{shlex.quote(ids['candidato'])}", check=True)
            promovido = True
            cambios.append(f"build promovida {a.candidato_sha[:12]}")
    except Exception as e:
        ctx.nota(f"no pude aplicar los cambios: {type(e).__name__}: {e}")
        return _rollback(ctx, resultados, backup, promovido, SALIDA_GATE)
    resultados["cambios"] = cambios
    ctx.log("cambios aplicados: " + (", ".join(cambios) or "ninguno"))

    # ------------------------------------------------------------ gates --
    try:
        ctx.espera(a.unidad, a.puerto_prod, limite=900)
    except ErrorInfraestructura as e:
        ctx.nota(str(e))
    if pasa_los_gates(ctx, resultados, "tras-aplicar"):
        resultados["aplicado"] = True
        return _cierra(ctx, resultados, SALIDA_FASE if fallos_fase else SALIDA_OK)

    ctx.nota("GATE ROJO tras aplicar: revierto unidad y build")
    return _rollback(ctx, resultados, backup, promovido, SALIDA_GATE)


def _rollback(ctx: Contexto, resultados: dict, backup: str, promovido: bool,
              codigo: int) -> int:
    """Deshace las DOS cosas. Restaurar la unidad no restaura la build (fallo D)."""
    resultados["aplicado"] = False
    resultados["rollback"] = {"unidad": backup, "build_revertida": promovido}
    ruta = ruta_unidad(ctx.unidad)
    sh(_sudo(f"cp {shlex.quote(backup)} {shlex.quote(ruta)}"))
    sh(_sudo(f"{SYSTEMCTL} daemon-reload"))
    if promovido:
        salida = sh(f"bash {shlex.quote(os.path.join(AQUI, 'builds.sh'))} volver")
        ctx.log(f"builds.sh volver: {salida.splitlines()[-1] if salida else '<sin salida>'}")
    sh(_sudo(f"{SYSTEMCTL} restart {ctx.unidad}"))
    try:
        sano = ctx.espera(ctx.unidad, ctx.puerto_prod, limite=900)
    except ErrorInfraestructura as e:
        ctx.nota(str(e))
        sano = False
    verde = sano and pasa_los_gates(ctx, resultados, "tras-rollback")
    resultados["rollback"]["produccion_sana"] = verde
    if not verde:
        ctx.nota("EL ROLLBACK NO DEJO PRODUCCION SANA: intervencion manual")
        return _cierra(ctx, resultados, SALIDA_ROLLBACK)
    return _cierra(ctx, resultados, codigo)


def _cierra(ctx: Contexto, resultados: dict, codigo: int) -> int:
    resultados["notas"] = ctx.notas
    resultados["estado_final"] = sh(f"{SYSTEMCTL} is-active {ctx.unidad}")
    resultados["codigo_salida"] = codigo
    resultados["fin"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ruta = os.path.join(ctx.dir_salida, "resultados.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(resultados, f, indent=1, ensure_ascii=False)
    ctx.log(f"estado final de {ctx.unidad}: {resultados['estado_final']!r}")
    ctx.log(f"resultados en {ruta} · salida {codigo}")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
