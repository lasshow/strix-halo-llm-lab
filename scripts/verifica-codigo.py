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
import json
import pathlib
import re
import shutil
import subprocess
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = json.loads((ROOT / "private" / "prompts.json").read_text())
VERIF = {p["id"]: p for p in PROMPTS if p.get("verif")}
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
            p = subprocess.run(cmd, cwd=str(self.dir), input=stdin, capture_output=True,
                               text=True, timeout=segundos + 30)
            return p.returncode, (p.stdout + p.stderr).strip()
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
    rc, o = caja.run(tsc + ["--target", "es2022", "--module", "commonjs",
                            "--outDir", str(caja.dir), str(f)])
    js = caja.dir / "a.js"
    rc2, existe = caja.run(["test", "-f", str(js)])
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


def v_sqlite(code, caja, pruebas):
    """Ejecuta la consulta contra el esquema del enunciado y comprueba el resultado."""
    caja.escribe("esquema.sql", SQL_ESQUEMA)
    rc, o = caja.run(["sqlite3", "t.db"], stdin=SQL_ESQUEMA)
    if rc:
        return "NO COMPILA", "fallo creando esquema: " + o
    sql = code if code.rstrip().endswith(";") else code + ";"
    rc, log = caja.run(["sqlite3", "-header", "-csv", "t.db"], stdin=sql)
    if rc:
        return "NO COMPILA", log
    # el enunciado pide por anio: 2024, 2025 y 2026, con el total bruto de 2024 = 8400
    filas = [l for l in log.splitlines() if l.strip()]
    anios = [a for a in ("2024", "2025", "2026") if any(a in f for f in filas)]
    if len(anios) != 3:
        return "COMPILA PERO FALLA", f"faltan anios (encontrados {anios}):\n{log}"
    if not any("8400" in f.replace(".0", "") for f in filas if "2024" in f):
        return "COMPILA PERO FALLA", f"el total bruto de 2024 deberia ser 8400:\n{log}"
    return "PASA LAS PRUEBAS", log


MOTORES = {"rustc": (v_rustc, ["rust"]), "tsc": (v_tsc, ["typescript", "ts"]),
           "node": (v_node, ["typescript", "ts"]), "python": (v_python, ["python", "py"]),
           "sqlite": (v_sqlite, ["sql", "sqlite"])}


def comprueba_sandbox():
    """No damos por hecho que aisla: se comprueba antes de ejecutar nada del LLM."""
    with Caja(sandbox=True) as c:
        rc, _ = c.run(["/bin/echo", "vale"], segundos=30)
        if rc != 0:
            return False, "systemd-run no arranca"
        rc, _ = c.run(["/usr/bin/getent", "hosts", "github.com"], segundos=30)
        if rc == 0:
            return False, "el sandbox TIENE RED"
        rc, _ = c.run(["/bin/cat", str(pathlib.Path.home() / ".secrets/m5-llama-api.key")], segundos=30)
        if rc == 0:
            return False, "el sandbox LEE los secretos del HOME"
        rc, _ = c.run(["/usr/bin/touch", "/usr/PWNED"], segundos=30)
        if rc == 0:
            return False, "el sandbox ESCRIBE en /usr"
    return True, "sin red, sin HOME, /usr solo lectura"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--etiqueta", required=True)
    ap.add_argument("--sin-sandbox", action="store_true",
                    help="SOLO depuracion: ejecuta con tus privilegios")
    a = ap.parse_args()

    usa_sandbox = not a.sin_sandbox
    if usa_sandbox:
        subprocess.run(["sudo", "-n", "mkdir", "-p", str(BASE_SANDBOX)], check=True)
        montados = monta_toolchains()
        print(f"[i] toolchains montadas ro en {TC}: {', '.join(montados)}")
        ok, detalle = comprueba_sandbox()
        if not ok:
            sys.exit(f"❌ El sandbox no aisla ({detalle}). Me niego a ejecutar codigo del LLM.")
        print(f"[i] sandbox verificado: {detalle}\n")
    else:
        print("⚠️  SIN SANDBOX: ejecutando codigo del LLM con tus privilegios.\n")

    src = ROOT / "private" / f"resp-{a.etiqueta}.json"
    res = json.loads(src.read_text())

    out = {}
    cuenta = {"PASA LAS PRUEBAS": 0, "COMPILA PERO FALLA": 0, "NO COMPILA": 0,
              "COMPILA (sin pruebas)": 0, "SIN RESPUESTA": 0, "HERRAMIENTA AUSENTE": 0}
    for pid, meta in VERIF.items():
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
        pruebas = PRUEBAS.get(pid, (None, None))[1]
        # H-024: un reventon del propio banco (sandbox que no arranca, disco
        # lleno, systemd-run que falla) NO es "NO COMPILA". Se separa el fallo
        # del modelo del fallo del instrumento.
        try:
            with Caja(sandbox=usa_sandbox) as caja:
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

    dst = ROOT / "private" / f"verif-{a.etiqueta}.json"
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
