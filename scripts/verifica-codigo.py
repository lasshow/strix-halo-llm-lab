#!/usr/bin/env python3
"""Verifica el codigo que devolvio el modelo: lo compila Y LO EJECUTA, aislado.

Dos correcciones sobre la version anterior (ver H-023):

1. AISLAMIENTO. Antes se ejecutaba codigo generado por un LLM con el usuario,
   la red, el HOME y las claves del que lanza el script. Ahora cada verificacion
   corre bajo `systemd-run` con usuario efimero (DynamicUser), sin red
   (PrivateNetwork), sin HOME (ProtectHome), con el sistema en solo lectura
   (ProtectSystem=strict) y con tope de memoria, tareas y tiempo.

   Trampa: DynamicUser implica PrivateTmp, asi que el directorio de trabajo
   NO puede estar en /tmp (dentro del sandbox se ve otro /tmp). Va en
   /var/lib/verif-sandbox.

2. "COMPILA" NO ES "FUNCIONA". Antes rustc compilaba con --crate-type lib: una
   funcion con la logica al reves aprobaba igual. Ahora cada prompt con banco de
   pruebas se ejecuta contra ASERCIONES sobre el enunciado, y el veredicto
   distingue tres estados: NO COMPILA / COMPILA PERO FALLA / PASA LAS PRUEBAS.

Uso:
  python3 scripts/verifica-codigo.py --etiqueta flashnext-v2
  python3 scripts/verifica-codigo.py --etiqueta flashnext-v2 --sin-sandbox  # solo depuracion
"""
import argparse
import csv
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUTA_PRIVADA = ROOT / "private" / "prompts.json"
RUTA_PUBLICA = ROOT / "benchmarks" / "bateria-publica.json"


class ErrorBanco(Exception):
    """El banco de pruebas no pudo evaluar: no dice nada sobre el codigo.

    Se distingue a proposito de un veredicto: un sandbox que no arranca, un
    compilador ausente o un timeout no son "NO COMPILA".
    """


def carga_bateria(ruta=None):
    """Carga los enunciados y devuelve {id: caso} con las claves normalizadas.

    Antes esto se hacia al importar el modulo leyendo private/prompts.json, asi
    que en una copia limpia del repo (sin datos privados) hasta `--help`
    reventaba con FileNotFoundError. Ahora se carga bajo demanda y la bateria
    publica es un origen de primera clase: su formato trae los campos
    `esperado_filas` / `pruebas` por caso, de modo que el flujo no depende de
    una seleccion codificada por identificadores antiguos (C1, C4, C5...).
    """
    if ruta is None:
        ruta = RUTA_PRIVADA if RUTA_PRIVADA.exists() else RUTA_PUBLICA
    ruta = pathlib.Path(ruta)
    if not ruta.exists():
        raise ErrorBanco(f"no encuentro la bateria en {ruta}. Usa --bateria "
                         f"para indicarla (publica: {RUTA_PUBLICA.name}).")
    d = json.loads(ruta.read_text())
    casos = d["prompts"] if isinstance(d, dict) else d
    fuera = {}
    for c in casos:
        cid = c.get("id")
        if not cid:
            continue
        # La bateria publica usa nombres largos; la privada, los cortos.
        norm = dict(c)
        norm["verif"] = c.get("verif") or c.get("verificador")
        norm["p"] = c.get("p") or c.get("prompt") or c.get("enunciado")
        norm["lang"] = c.get("lang") or c.get("lenguaje")
        norm["pruebas"] = c.get("pruebas") or c.get("tests")
        norm["esperado_filas"] = c.get("esperado_filas")
        norm["esquema"] = c.get("esquema")
        # La bateria publica no trae 'verif': se deriva del lenguaje, para que
        # un caso publico se pueda verificar sin tabla codificada por id.
        if not norm["verif"] and norm["lang"]:
            norm["verif"] = {"sql": "sqlite", "sqlite": "sqlite",
                             "python": "python", "py": "python",
                             "rust": "rust", "typescript": "node",
                             "ts": "node", "javascript": "node",
                             "go": "go", "zig": "zig"}.get(norm["lang"].lower())
        norm["origen"] = str(ruta)
        fuera[cid] = norm
    return fuera


BASE_SANDBOX = pathlib.Path("/var/lib/verif-sandbox")

# Las toolchains viven en el HOME (cargo, nvm, node_modules) y ProtectHome=yes las
# tapa: dentro del sandbox /home esta vacio, asi que un BindReadOnlyPaths cuyo
# ORIGEN este en /home tampoco sobrevive (da rc=203, exec no encontrado). Solucion:
# bind mounts de solo lectura hechos en el HOST hacia /opt/tc, fuera de /home.
TC = pathlib.Path("/opt/tc")
MOUNTS = {
    TC / "cargo": pathlib.Path.home() / ".cargo",
    TC / "rustup": pathlib.Path.home() / ".rustup",
    TC / "nvm": pathlib.Path.home() / ".nvm",
    TC / "node_modules": pathlib.Path.home() / ".hermes/hermes-agent/node_modules",
}
NODE = TC / "nvm/versions/node/v22.23.1/bin/node"
RUSTC = TC / "cargo/bin/rustc"
TSC = TC / "node_modules/typescript/bin/tsc"
# El python del venv enlaza a librerias del HOME: dentro del sandbox se usa el del sistema.
PYSANDBOX = "/usr/bin/python3"


def monta_toolchains():
    """Bind mounts ro en el host. Idempotente."""
    import shutil as _sh
    hechos = []
    for destino, origen in MOUNTS.items():
        if not origen.exists():
            continue
        subprocess.run(["sudo", "-n", "mkdir", "-p", str(destino)], check=True)
        ya = subprocess.run(["findmnt", "-rno", "TARGET", str(destino)],
                            capture_output=True, text=True).returncode == 0
        if not ya:
            subprocess.run(["sudo", "-n", "mount", "--bind", "-o", "ro",
                            str(origen), str(destino)], check=True)
        hechos.append(destino.name)
    return hechos

# --- Bancos de pruebas: aserciones derivadas del enunciado de cada prompt ----
# Se anexan al codigo del modelo. Si el codigo compila pero da resultados
# incorrectos, esto falla. Es la diferencia entre "compila" y "funciona".
PRUEBAS = {
    "C1": ("rust", '''
fn main() {
    assert_eq!(parse_iso_duration("PT1H30M15S"), Some(5415), "PT1H30M15S");
    assert_eq!(parse_iso_duration("PT30M"), Some(1800), "PT30M");
    assert_eq!(parse_iso_duration("PT15S"), Some(15), "PT15S");
    assert_eq!(parse_iso_duration("PT2H"), Some(7200), "PT2H");
    assert!(parse_iso_duration("basura").is_none(), "entrada invalida -> None");
    assert!(parse_iso_duration("").is_none(), "vacio -> None");
    println!("PRUEBAS-OK");
}
'''),
    "C4": ("ts", '''
(async () => {
  async function* fuente(): AsyncGenerator<number> { for (let i = 1; i <= 7; i++) yield i; }
  const salida: number[][] = [];
  for await (const g of chunked(fuente(), 3)) salida.push(g);
  const esperado = JSON.stringify([[1,2,3],[4,5,6],[7]]);
  if (JSON.stringify(salida) !== esperado) {
    throw new Error("agrupado mal: " + JSON.stringify(salida) + " != " + esperado);
  }
  async function* vacia(): AsyncGenerator<number> {}
  const s2: number[][] = [];
  for await (const g of chunked(vacia(), 3)) s2.push(g);
  if (s2.length !== 0) throw new Error("iterable vacio deberia dar 0 grupos");
  console.log("PRUEBAS-OK");
})();
'''),
    "C5": ("python", '''
import tempfile, os, collections
_d = tempfile.mkdtemp()
_p = os.path.join(_d, "log.txt")
open(_p, "w").write("a\\nb\\na\\nc\\nb\\na\\n")
_r = top_n(_p, 2)
assert [t[0] for t in _r] == ["a", "b"], f"orden/claves mal: {_r}"
assert _r[0][1] == 3 and _r[1][1] == 2, f"cuentas mal: {_r}"
assert len(top_n(_p, 10)) == 3, "n mayor que el numero de claves"
print("PRUEBAS-OK")
'''),
}
# C6 (SQL) se valida ejecutando la consulta y comprobando el resultado.
SQL_ESQUEMA = (
    "CREATE TABLE nominas(fecha TEXT, bruto REAL, liquido REAL);\n"
    "INSERT INTO nominas VALUES"
    "('2024-01-31',4200.0,3100.0),('2024-02-29',4200.0,3100.0),"
    "('2025-01-31',4800.0,3500.0),('2025-02-28',4900.0,3560.0),"
    "('2026-01-31',5200.0,3800.0);\n"
)


def bloque(txt, langs):
    """Extrae el primer bloque de codigo; si no hay vallas, devuelve el texto."""
    for lg in langs:
        m = re.search(rf"```{lg}\s*\n(.*?)```", txt, re.S | re.I)
        if m:
            return m.group(1)
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", txt, re.S)
    return m.group(1) if m else txt


# Codigos con que systemd senala que NO llego a ejecutar el binario. Ver
# systemd.exec(5): 203 EXEC (ejecutable no disponible o no ejecutable),
# 200-242 estan reservados para fallos de preparacion del servicio.
LANZADOR_FALLO = frozenset({203, 208, 209, 210, 212, 216, 217, 218, 219,
                            220, 221, 222, 224, 225, 226, 227, 228, 229,
                            230, 231, 232, 233, 235, 236, 237, 238, 239,
                            240, 241, 242})

_SENALES_LANZADOR = (
    "Failed to start transient service",
    "Failed to connect to bus",
    "sudo: a password is required",
    "sudo: command not found",
    "systemd-run: command not found",
)


def _huele_a_lanzador(salida):
    """Fallos del lanzador que no traen un codigo reservado."""
    return any(s in salida for s in _SENALES_LANZADOR)


class Caja:
    """Ejecuta ordenes aisladas. Si sandbox=False cae al comportamiento antiguo."""

    def __init__(self, sandbox=True):
        self.sandbox = sandbox
        self.dir = None

    def __enter__(self):
        nombre = f"job-{uuid.uuid4().hex[:12]}"
        if self.sandbox:
            self.dir = BASE_SANDBOX / nombre
            subprocess.run(["sudo", "-n", "mkdir", "-p", str(self.dir)], check=True)
            subprocess.run(["sudo", "-n", "chmod", "1777", str(self.dir)], check=True)
        else:
            self.dir = pathlib.Path("/tmp") / nombre
            self.dir.mkdir(parents=True)
        return self

    def __exit__(self, *a):
        if self.dir and self.dir.exists():
            subprocess.run(["sudo", "-n", "rm", "-rf", str(self.dir)] if self.sandbox
                           else ["rm", "-rf", str(self.dir)], check=False)
        return False

    def escribe(self, nombre, texto):
        d = self.dir / nombre
        if self.sandbox:
            subprocess.run(["sudo", "-n", "tee", str(d)], input=texto,
                           text=True, capture_output=True, check=True)
            subprocess.run(["sudo", "-n", "chmod", "666", str(d)], check=True)
        else:
            d.write_text(texto)
        return d

    def run(self, cmd, stdin=None, segundos=180):
        if not self.sandbox:
            try:
                p = subprocess.run(cmd, cwd=str(self.dir), input=stdin,
                                   capture_output=True, text=True,
                                   timeout=segundos + 30)
            except FileNotFoundError as e:
                # Herramienta ausente: el banco no pudo evaluar. NO es "NO COMPILA".
                raise ErrorBanco(f"no encuentro el ejecutable {cmd[0]!r}: {e}") from e
            salida = (p.stdout + p.stderr).strip()
            # Mismo criterio que en la rama con sandbox: un fallo del lanzador
            # no es un veredicto sobre el codigo del modelo.
            if p.returncode in LANZADOR_FALLO or _huele_a_lanzador(salida):
                raise ErrorBanco(
                    f"el lanzador no pudo ejecutar {cmd[0]!r} "
                    f"(codigo {p.returncode}): {salida[:300]}")
            return p.returncode, salida
        pre = [
            "sudo", "-n", "systemd-run", "--quiet", "--pipe", "--collect",
            "--service-type=exec",
            "--property=DynamicUser=yes",
            "--property=PrivateNetwork=yes",      # sin red: no puede llamar a casa
            "--property=ProtectHome=yes",         # sin /home: no ve claves ni datos
            "--property=ProtectSystem=strict",    # todo el sistema en solo lectura
            "--property=NoNewPrivileges=yes",
            "--property=ProtectKernelTunables=yes",
            "--property=ProtectControlGroups=yes",
            "--property=RestrictSUIDSGID=yes",
            "--property=MemoryMax=2G",
            "--property=TasksMax=256",
            f"--property=RuntimeMaxSec={segundos}",
            f"--property=ReadWritePaths={self.dir}",
            f"--property=WorkingDirectory={self.dir}",
            f"--property=Environment=RUSTUP_HOME={TC}/rustup",
            f"--property=Environment=CARGO_HOME={TC}/cargo",
            f"--property=Environment=HOME={self.dir}",
        ]
        p = subprocess.run(pre + cmd, input=stdin, capture_output=True,
                           text=True, timeout=segundos + 60)
        salida = (p.stdout + p.stderr).strip()
        if p.returncode == 143 or "RuntimeMaxSec" in salida:
            return p.returncode, salida + "\n[sandbox] excedio el tiempo limite"
        # Un fallo del LANZADOR no es un veredicto sobre el codigo. systemd
        # devuelve estos codigos cuando no ha llegado a ejecutar el binario
        # (203/EXEC = ejecutable no disponible, 226 = fallo de namespace...), y
        # tratarlos como returncode del compilador producia "NO COMPILA" sin
        # haber compilado nada. No todos los fallos de systemd-run lanzan
        # excepcion Python: la mayoria son un codigo numerico.
        if p.returncode in LANZADOR_FALLO or _huele_a_lanzador(salida):
            raise ErrorBanco(
                f"el lanzador no pudo ejecutar {cmd[0]!r} (codigo {p.returncode}): "
                f"{salida[:300]}")
        return p.returncode, salida


# --- Motores: devuelven (estado, log) -----------------------------------------
# estado: "NO COMPILA" | "COMPILA PERO FALLA" | "PASA LAS PRUEBAS" | "COMPILA (sin pruebas)"
def v_rustc(code, caja, pruebas):
    rustc = str(RUSTC) if caja.sandbox else "rustc"
    if pruebas:
        f = caja.escribe("main.rs", code + "\n" + pruebas)
        binario = caja.dir / "prog"
        rc, log = caja.run([rustc, "--edition", "2021", "-o", str(binario), str(f)])
        if rc:
            return "NO COMPILA", log
        rc, log = caja.run([str(binario)])
        if rc == 0 and "PRUEBAS-OK" in log:
            return "PASA LAS PRUEBAS", log
        return "COMPILA PERO FALLA", log
    f = caja.escribe("lib.rs", code)
    rc, log = caja.run([rustc, "--edition", "2021", "--crate-type", "lib",
                        "-o", "out.rlib", str(f)])
    if rc and "expected item" in log:
        f = caja.escribe("main.rs", "fn main() {\n" + code + "\n}\n")
        rc, log = caja.run([rustc, "--edition", "2021", str(f), "-o", "out"])
        if rc == 0:
            log = "(envuelto en fn main: la respuesta era un fragmento)\n" + log
    return ("COMPILA (sin pruebas)" if rc == 0 else "NO COMPILA"), log


def v_tsc(code, caja, pruebas):
    f = caja.escribe("a.ts", code)
    tsc = [str(NODE), str(TSC)] if caja.sandbox else ["tsc"]
    rc, log = caja.run(tsc + [ "--strict", "--target", "es2022", "--module", "esnext",
                        "--moduleResolution", "bundler", "--noEmit", str(f)])
    return ("COMPILA (sin pruebas)" if rc == 0 else "NO COMPILA"), log


def v_node(code, caja, pruebas):
    f = caja.escribe("a.ts", code + ("\n" + pruebas if pruebas else ""))
    tsc = [str(NODE), str(TSC)] if caja.sandbox else ["tsc"]
    node = str(NODE) if caja.sandbox else "node"
    # --noEmitOnError es imprescindible: por defecto tsc EMITE JavaScript aunque
    # el tipado falle, asi que comprobar solo "existe a.js" daba PASA LAS
    # PRUEBAS a codigo con errores reales de compilacion (p.ej. TS2322,
    # asignar string a number) cuyo JS resultante se ejecuta sin problema.
    rc, o = caja.run(tsc + ["--noEmitOnError", "--strict",
                            "--target", "es2022", "--module", "commonjs",
                            "--outDir", str(caja.dir), str(f)])
    if rc != 0:
        return "NO COMPILA", "tsc fallo (codigo %d):\n%s" % (rc, o)
    js = caja.dir / "a.js"
    rc2, _existe = caja.run(["test", "-f", str(js)])
    if rc2 != 0:
        return "NO COMPILA", "tsc no genero JS:\n" + o
    rc, log = caja.run([node, str(js)])
    if not pruebas:
        return ("COMPILA (sin pruebas)" if rc == 0 else "COMPILA PERO FALLA"), log
    if rc == 0 and "PRUEBAS-OK" in log:
        return "PASA LAS PRUEBAS", log
    return "COMPILA PERO FALLA", log


def v_python(code, caja, pruebas):
    py = PYSANDBOX if caja.sandbox else sys.executable
    if pruebas:
        f = caja.escribe("a.py", code + "\n" + pruebas)
        rc, log = caja.run([py, str(f)])
        if rc == 0 and "PRUEBAS-OK" in log:
            return "PASA LAS PRUEBAS", log
        # distinguir error de sintaxis de fallo de logica
        f2 = caja.escribe("solo.py", code)
        rc2, _ = caja.run([py, "-m", "py_compile", str(f2)])
        return ("NO COMPILA" if rc2 else "COMPILA PERO FALLA"), log
    f = caja.escribe("a.py", code)
    rc, log = caja.run([py, "-m", "py_compile", str(f)])
    return ("COMPILA (sin pruebas)" if rc == 0 else "NO COMPILA"), log


def v_sqlite(code, caja, pruebas, esperado_filas=None, esquema=None):
    """Ejecuta la consulta contra el esquema del enunciado y compara resultados.

    Antes se buscaban subcadenas ("aparece 2024", "aparece 8400"), asi que
    `SELECT '2024 2025 2026 84000' AS basura;` pasaba las pruebas sin consultar
    ni una tabla. Ahora hay dos modos, ambos exigiendo valores y no textos:

    - `esperado_filas`: comparacion de filas completas (bateria publica, donde
      el resultado es cerrado). El esquema viaja con el caso.
    - sin esperado: comprobacion estructurada de C6. No se comparan filas
      literales porque "% de retencion medio" admite dos lecturas legitimas
      (agregada 27,216 % vs media por nomina 27,215 %); se exigen los agregados
      exactos y los porcentajes con tolerancia declarada.
    """
    esq = esquema or SQL_ESQUEMA
    caja.escribe("esquema.sql", esq)
    rc, o = caja.run(["sqlite3", "t.db"], stdin=esq)
    if rc:
        return "NO COMPILA", "fallo creando esquema: " + o
    sql = code if code.rstrip().endswith(";") else code + ";"
    rc, log = caja.run(["sqlite3", "-noheader", "-csv", "t.db"], stdin=sql)
    if rc:
        return "NO COMPILA", log
    filas = [f for f in csv.reader(io.StringIO(log)) if any(c.strip() for c in f)]

    if esperado_filas is not None:
        if _normaliza_filas(filas) != _normaliza_filas(esperado_filas):
            return "COMPILA PERO FALLA", (
                "filas distintas de las esperadas.\nesperado: %r\nobtenido: %r"
                % (esperado_filas, filas))
        return "PASA LAS PRUEBAS", log

    return _comprueba_c6(filas, log)


# Agregados calculados del esquema, no copiados del enunciado:
#   ano: (total bruto, total liquido, retencion %, variacion % interanual)
# La variacion del primer ano no esta definida (NULL o vacio, ambos validos).
C6_ESPERADO = {
    "2024": (8400.0, 6200.0, 26.19, None),
    "2025": (9700.0, 7060.0, 27.22, 15.48),
    "2026": (5200.0, 3800.0, 26.92, -46.39),
}
# Consulta de referencia del caso C6: es la que produce C6_ESPERADO al
# ejecutarse contra SQL_ESQUEMA. Se publica para que el esperado sea
# reproducible y no un numero copiado a mano.
C6_CONSULTA = (
    "SELECT substr(fecha,1,4) AS anio, "
    "SUM(bruto) AS bruto, "
    "SUM(liquido) AS liquido, "
    "ROUND((1 - SUM(liquido)*1.0/SUM(bruto)) * 100, 2) AS retencion, "
    "ROUND((SUM(bruto)*1.0/LAG(SUM(bruto)) OVER (ORDER BY substr(fecha,1,4)) "
    "- 1) * 100, 2) AS var "
    "FROM nominas GROUP BY anio ORDER BY anio;"
)
C6_TOLERANCIA = 0.05   # puntos porcentuales: cubre las dos lecturas de "medio"


def _num(celda):
    try:
        return float(str(celda).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None


def _normaliza_filas(filas):
    """Compara por valor: 8400 == 8400.0 == ' 8400 ', pero no por subcadena."""
    fuera = []
    for f in filas:
        fila = []
        for c in f:
            v = _num(c)
            fila.append(round(v, 4) if v is not None else str(c).strip())
        fuera.append(tuple(fila))
    return fuera


def _comprueba_c6(filas, log):
    if len(filas) != 3:
        return "COMPILA PERO FALLA", f"esperaba 3 filas (una por anio), hay {len(filas)}:\n{log}"
    for f in filas:
        if len(f) < 5:
            return "COMPILA PERO FALLA", (
                f"cada fila necesita anio, bruto, liquido, retencion y variacion; "
                f"llegaron {len(f)} columnas: {f!r}")
    vistos = []
    for f in filas:
        ano = str(f[0]).strip()[:4]
        if ano not in C6_ESPERADO:
            return "COMPILA PERO FALLA", f"anio inesperado {f[0]!r}:\n{log}"
        vistos.append(ano)
        e_bruto, e_liq, e_ret, e_var = C6_ESPERADO[ano]
        bruto, liq, ret, var = (_num(f[1]), _num(f[2]), _num(f[3]), _num(f[4]))
        if bruto is None or abs(bruto - e_bruto) > 0.01:
            return "COMPILA PERO FALLA", f"{ano}: bruto {f[1]!r}, esperaba {e_bruto}"
        if liq is None or abs(liq - e_liq) > 0.01:
            return "COMPILA PERO FALLA", f"{ano}: liquido {f[2]!r}, esperaba {e_liq}"
        if ret is None or abs(ret - e_ret) > C6_TOLERANCIA:
            return "COMPILA PERO FALLA", (
                f"{ano}: retencion {f[3]!r}, esperaba {e_ret} +-{C6_TOLERANCIA}")
        if e_var is None:
            if var is not None and abs(var) > 0.01:
                return "COMPILA PERO FALLA", (
                    f"{ano}: la variacion del primer anio no esta definida, llego {f[4]!r}")
        elif var is None or abs(var - e_var) > C6_TOLERANCIA:
            return "COMPILA PERO FALLA", (
                f"{ano}: variacion {f[4]!r}, esperaba {e_var} +-{C6_TOLERANCIA}")
    if sorted(vistos) != ["2024", "2025", "2026"]:
        return "COMPILA PERO FALLA", f"anios {vistos}, esperaba 2024/2025/2026"
    if vistos != sorted(vistos):
        return "COMPILA PERO FALLA", f"el enunciado pide orden por anio, llego {vistos}"
    return "PASA LAS PRUEBAS", log


MOTORES = {"rustc": (v_rustc, ["rust"]), "tsc": (v_tsc, ["typescript", "ts"]),
           "node": (v_node, ["typescript", "ts"]), "python": (v_python, ["python", "py"]),
           "sqlite": (v_sqlite, ["sql", "sqlite"])}


def comprueba_sandbox():
    """No damos por hecho que aisla: se comprueba antes de ejecutar nada del LLM.

    Devuelve (veredicto, motivo) donde veredicto es True (aisla), False (NO
    aisla) o None (INCONCLUSO). El tercer estado es imprescindible: antes, un
    comando de diagnostico que no llegaba a ejecutarse devolvia rc!=0 y eso se
    leia como "no hay red" / "no lee el secreto" / "no escribe", es decir, una
    prueba que no se ejecuto certificaba seguridad. Ahora cada sonda exige que
    su herramienta exista y que el fallo sea el esperado.
    """
    with Caja(sandbox=True) as c:
        try:
            rc, salida = c.run(["/bin/echo", "vale"], segundos=30)
        except ErrorBanco as e:
            return None, f"INCONCLUSO: el sandbox no arranca ({e})"
        if rc != 0 or "vale" not in salida:
            return None, f"INCONCLUSO: systemd-run no ejecuta ni /bin/echo (rc={rc})"

        def sonda(cmd, nombre):
            """Devuelve (rc, salida) o marca inconcluso si la herramienta falta."""
            existe, _ = c.run(["test", "-x", cmd[0]], segundos=30)
            if existe != 0:
                raise ErrorBanco(f"{nombre}: {cmd[0]} no esta en el sandbox, "
                                 "la sonda no puede ejecutarse")
            return c.run(cmd, segundos=30)

        secreto = pathlib.Path.home() / ".secrets/m5-llama-api.key"
        try:
            rc, _ = sonda(["/usr/bin/getent", "hosts", "github.com"], "red")
            if rc == 0:
                return False, "el sandbox TIENE RED"
            if not secreto.exists():
                return None, ("INCONCLUSO: no existe el fichero senuelo "
                              f"{secreto.name}, la sonda del secreto no prueba nada")
            rc, _ = sonda(["/bin/cat", str(secreto)], "secreto")
            if rc == 0:
                return False, "el sandbox LEE los secretos del HOME"
            rc, _ = sonda(["/usr/bin/touch", "/usr/PWNED"], "escritura")
            if rc == 0:
                return False, "el sandbox ESCRIBE en /usr"
        except ErrorBanco as e:
            return None, f"INCONCLUSO: {e}"
    return True, "sin red, sin HOME, /usr solo lectura"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--etiqueta", required=True)
    ap.add_argument("--bateria", default=None,
                    help="JSON de enunciados. Por defecto private/prompts.json "
                         "si existe, si no benchmarks/bateria-publica.json")
    ap.add_argument("--respuestas", default=None,
                    help="JSON de respuestas del modelo (por defecto "
                         "private/resp-<etiqueta>.json)")
    ap.add_argument("--sin-sandbox", action="store_true",
                    help="SOLO depuracion: ejecuta con tus privilegios")
    ap.add_argument("--salida", default=None,
                    help="donde escribir el informe (por defecto "
                         "private/verif-<etiqueta>.json)")
    ap.add_argument("--solo", default="",
                    help="ids separados por coma: verifica solo esos casos. "
                         "Sin esto, un caso sin respuesta cuenta como "
                         "'no evaluado por el banco' (salida 3), que es lo "
                         "correcto en una campana completa.")
    a = ap.parse_args()

    # Se carga DESPUES de parsear: asi --help funciona en una copia limpia sin
    # los datos privados, que antes reventaba al importar el modulo.
    try:
        casos = carga_bateria(a.bateria)
    except ErrorBanco as e:
        sys.exit(f"❌ {e}")
    verif = {cid: c for cid, c in casos.items() if c.get("verif")}
    if a.solo:
        quiere = {s.strip() for s in a.solo.split(",")}
        desconocidos = quiere - set(verif)
        if desconocidos:
            sys.exit(f"❌ ids sin verificador en la bateria: {sorted(desconocidos)}")
        verif = {cid: c for cid, c in verif.items() if cid in quiere}
    if not verif:
        sys.exit("❌ la bateria no trae ningun caso con verificador")
    print(f"[i] bateria: {next(iter(verif.values()))['origen']} "
          f"({len(verif)} casos con verificador)")

    usa_sandbox = not a.sin_sandbox
    if usa_sandbox:
        subprocess.run(["sudo", "-n", "mkdir", "-p", str(BASE_SANDBOX)], check=True)
        montados = monta_toolchains()
        print(f"[i] toolchains montadas ro en {TC}: {', '.join(montados)}")
        ok, detalle = comprueba_sandbox()
        if ok is None:
            # Inconcluso NO es aprobado: una comprobacion que no pudo
            # ejecutarse no certifica aislamiento.
            sys.exit(f"❌ No se pudo verificar el aislamiento ({detalle}). "
                     "Me niego a ejecutar codigo del LLM sin certeza.")
        if not ok:
            sys.exit(f"❌ El sandbox no aisla ({detalle}). Me niego a ejecutar codigo del LLM.")
        print(f"[i] sandbox verificado: {detalle}\n")
    else:
        print("⚠️  SIN SANDBOX: ejecutando codigo del LLM con tus privilegios.\n")

    src = pathlib.Path(a.respuestas) if a.respuestas else (
        ROOT / "private" / f"resp-{a.etiqueta}.json")
    if not src.exists():
        sys.exit(f"❌ no encuentro las respuestas en {src} (usa --respuestas)")
    res = json.loads(src.read_text())

    out = {}
    cuenta = {"PASA LAS PRUEBAS": 0, "COMPILA PERO FALLA": 0, "NO COMPILA": 0,
              "COMPILA (sin pruebas)": 0, "SIN RESPUESTA": 0, "HERRAMIENTA AUSENTE": 0}
    for pid, meta in verif.items():
        r = res.get(pid)
        if not r or "error" in r or not (r.get("texto") or "").strip():
            out[pid] = {"veredicto": "SIN RESPUESTA"}
            cuenta["SIN RESPUESTA"] += 1
            print(f"{pid:3} [{meta['verif']:6}] SIN RESPUESTA")
            continue
        fn, langs = MOTORES[meta["verif"]]
        if usa_sandbox:
            rutas = {"rustc": RUSTC, "tsc": TSC, "node": NODE,
                     "python": pathlib.Path(PYSANDBOX), "sqlite": pathlib.Path("/usr/bin/sqlite3")}
            falta = not rutas[meta["verif"]].exists()
        else:
            binario = {"python": sys.executable, "sqlite": "sqlite3"}.get(meta["verif"], meta["verif"])
            falta = shutil.which(binario) is None
        if falta:
            out[pid] = {"veredicto": "HERRAMIENTA AUSENTE"}
            cuenta["HERRAMIENTA AUSENTE"] += 1
            print(f"{pid:3} [{meta['verif']:6}] herramienta ausente, salto")
            continue
        code = bloque(r["texto"], langs)
        # Las pruebas vienen del propio caso si la bateria las trae (formato
        # publico); si no, de la tabla PRUEBAS heredada por identificador.
        pruebas = meta.get("pruebas") or PRUEBAS.get(pid, (None, None))[1]
        # H-024: un reventon del propio banco (sandbox que no arranca, disco
        # lleno, systemd-run que falla) NO es "NO COMPILA". Se separa el fallo
        # del modelo del fallo del instrumento.
        try:
            with Caja(sandbox=usa_sandbox) as caja:
                if meta["verif"] == "sqlite" and meta.get("esperado_filas") is not None:
                    estado, log = fn(code, caja, pruebas,
                                     esperado_filas=meta["esperado_filas"],
                                     esquema=meta.get("esquema"))
                else:
                    estado, log = fn(code, caja, pruebas)
        except Exception as e:
            estado, log = "ERROR DEL BANCO", f"{type(e).__name__}: {e}"
        cuenta[estado] = cuenta.get(estado, 0) + 1
        out[pid] = {"veredicto": estado, "con_pruebas": bool(pruebas),
                    "log": log[:1500], "lineas_codigo": code.count("\n") + 1}
        marca = {"PASA LAS PRUEBAS": "[OK]", "COMPILA (sin pruebas)": "[~]",
                 "COMPILA PERO FALLA": "[X]", "NO COMPILA": "[X]",
                 "ERROR DEL BANCO": "[!]"}.get(estado, "?")
        print(f"{pid:3} [{meta['verif']:6}] {marca} {estado}"
              + ("" if estado.startswith(("PASA", "COMPILA (")) or not log
                 else f"\n      {log.splitlines()[0][:150]}"))

    dst = pathlib.Path(a.salida) if a.salida else (
        ROOT / "private" / f"verif-{a.etiqueta}.json")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n{'-'*54}")
    print(f"  PASA LAS PRUEBAS      {cuenta['PASA LAS PRUEBAS']}   (compila y da el resultado correcto)")
    print(f"  COMPILA (sin pruebas) {cuenta['COMPILA (sin pruebas)']}   (no hay banco de pruebas para este)")
    print(f"  COMPILA PERO FALLA    {cuenta['COMPILA PERO FALLA']}   (el compilador lo acepta, el resultado es malo)")
    print(f"  NO COMPILA            {cuenta['NO COMPILA']}")
    # Estos tres NO son culpa del modelo: son del banco de pruebas.
    infra = (cuenta["SIN RESPUESTA"] + cuenta["HERRAMIENTA AUSENTE"]
             + cuenta.get("ERROR DEL BANCO", 0))
    if infra:
        print(f"  --- no evaluados por el banco: {infra} "
              f"(sin respuesta {cuenta['SIN RESPUESTA']}, "
              f"herramienta ausente {cuenta['HERRAMIENTA AUSENTE']}, "
              f"error del banco {cuenta.get('ERROR DEL BANCO', 0)})")
    print(f"{'-'*54}\n-> {dst}")

    # Codigo de salida (H-024: antes salia 0 pasara lo que pasara).
    #   0 todo evaluado y sin fallos del modelo
    #   2 el modelo fallo en algun caso
    #   3 el banco no pudo evaluar algun caso (mas grave: no hay veredicto)
    if infra:
        return 3
    if cuenta["NO COMPILA"] or cuenta["COMPILA PERO FALLA"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
