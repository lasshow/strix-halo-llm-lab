#!/usr/bin/env python3
"""Verifica compilando/ejecutando el codigo que devolvio el modelo.

Regla del laboratorio: el codigo no se corrige "a ojo". Se extrae el bloque
de la respuesta, se compila (rustc / tsc / node / python) o se ejecuta
(sqlite3) y se guarda el veredicto real del compilador.

Uso:
  python3 scripts/verifica-codigo.py --etiqueta flashnext-v2
"""
import argparse, json, pathlib, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = json.loads((ROOT / "private" / "prompts.json").read_text())
VERIF = {p["id"]: p for p in PROMPTS if p.get("verif")}


def bloque(txt, langs):
    """Extrae el primer bloque de codigo; si no hay vallas, devuelve el texto."""
    for lg in langs:
        m = re.search(rf"```{lg}\s*\n(.*?)```", txt, re.S | re.I)
        if m:
            return m.group(1)
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", txt, re.S)
    return m.group(1) if m else txt


def run(cmd, cwd=None, stdin=None):
    p = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True,
                       text=True, timeout=180)
    return p.returncode, (p.stdout + p.stderr).strip()


def v_rustc(code, d):
    """Compila como libreria; si la respuesta es un fragmento de sentencias
    (let/if/for sueltos, valido como respuesta al enunciado) se reintenta
    envuelto en fn main(). El veredicto es del compilador, no del corrector."""
    f = d / "lib.rs"
    f.write_text(code)
    rc, log = run(["rustc", "--edition", "2021", "--crate-type", "lib",
                   "-o", str(d / "out.rlib"), str(f)])
    if rc and "expected item" in log:
        f.write_text("fn main() {\n" + code + "\n}\n")
        rc, log = run(["rustc", "--edition", "2021", str(f),
                       "-o", str(d / "out")])
        if rc == 0:
            log = "(envuelto en fn main: la respuesta era un fragmento)\n" + log
    return rc, log


def v_tsc(code, d):
    f = d / "a.ts"
    f.write_text(code)
    return run(["tsc", "--strict", "--target", "es2022", "--module", "esnext",
                "--moduleResolution", "bundler", "--noEmit", str(f)])


def v_node(code, d):
    """TS que ademas debe EJECUTARSE: se transpila sin comprobar tipos."""
    f = d / "a.ts"
    f.write_text(code)
    rc, o = run(["tsc", "--target", "es2022", "--module", "commonjs",
                 "--outDir", str(d), str(f)])
    js = d / "a.js"
    if not js.exists():
        return rc or 1, "tsc no genero JS:\n" + o
    return run(["node", str(js)])


def v_python(code, d):
    f = d / "a.py"
    f.write_text(code)
    return run([sys.executable, "-m", "py_compile", str(f)])


def v_sqlite(code, d):
    """Crea el esquema del enunciado y ejecuta el SQL del modelo."""
    esquema = (
        "CREATE TABLE nominas(fecha TEXT, bruto REAL, liquido REAL);\n"
        "INSERT INTO nominas VALUES"
        "('2024-01-31',4200.0,3100.0),('2024-02-29',4200.0,3100.0),"
        "('2025-01-31',4800.0,3500.0),('2025-02-28',4900.0,3560.0),"
        "('2026-01-31',5200.0,3800.0);\n"
    )
    db = d / "t.db"
    rc, o = run(["sqlite3", str(db)], stdin=esquema)
    if rc:
        return rc, "fallo creando esquema: " + o
    return run(["sqlite3", "-header", "-column", str(db)],
               stdin=code if code.rstrip().endswith(";") else code + ";")


MOTORES = {"rustc": (v_rustc, ["rust"]), "tsc": (v_tsc, ["typescript", "ts"]),
           "node": (v_node, ["typescript", "ts"]), "python": (v_python, ["python", "py"]),
           "sqlite": (v_sqlite, ["sql", "sqlite"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--etiqueta", required=True)
    a = ap.parse_args()
    src = ROOT / "private" / f"resp-{a.etiqueta}.json"
    res = json.loads(src.read_text())

    out, ok = {}, 0
    for pid, meta in VERIF.items():
        r = res.get(pid)
        if not r or "error" in r or not (r.get("texto") or "").strip():
            out[pid] = {"veredicto": "SIN RESPUESTA"}
            print(f"{pid:3} [{meta['verif']:6}] SIN RESPUESTA")
            continue
        fn, langs = MOTORES[meta["verif"]]
        if shutil.which({"python": sys.executable}.get(meta["verif"], meta["verif"].replace("sqlite", "sqlite3"))) is None \
                and meta["verif"] != "python":
            out[pid] = {"veredicto": "HERRAMIENTA AUSENTE"}
            print(f"{pid:3} [{meta['verif']:6}] herramienta ausente, salto")
            continue
        code = bloque(r["texto"], langs)
        with tempfile.TemporaryDirectory() as td:
            rc, log = fn(code, pathlib.Path(td))
        ok += rc == 0
        out[pid] = {"veredicto": "COMPILA/EJECUTA" if rc == 0 else "FALLA",
                    "rc": rc, "log": log[:1500], "lineas_codigo": code.count("\n") + 1}
        print(f"{pid:3} [{meta['verif']:6}] {out[pid]['veredicto']}"
              + ("" if rc == 0 else f"\n      {log.splitlines()[0][:150] if log else ''}"))

    dst = ROOT / "private" / f"verif-{a.etiqueta}.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n{ok}/{len(VERIF)} pasan el compilador -> {dst}")


if __name__ == "__main__":
    sys.exit(main())
