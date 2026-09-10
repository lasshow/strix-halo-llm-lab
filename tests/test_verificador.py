"""Pruebas del verificador de codigo: los falsos positivos de la 2a auditoria.

Las 52 pruebas anteriores no ejercitaban `verifica-codigo.py`, y por eso podian
estar todas verdes conviviendo con dos aprobados falsos:

  - TypeScript: `tsc` salia con codigo 2 por un error de tipos, pero como
    emite el .js igualmente (noEmitOnError esta desactivado por defecto), el
    verificador ejecutaba ese .js, las aserciones pasaban y el veredicto era
    PASA LAS PRUEBAS con un error real del compilador.
  - SQL: se comprobaba que la salida CONTUVIERA ciertos numeros, asi que
    `SELECT '2024 2025 2026 84000' AS basura` aprobaba.

Estas pruebas usan tsc/node/sqlite3 reales del entorno (no simulan el
compilador) y se saltan solas si falta la herramienta: un compilador ausente
tiene que ser inconcluso, nunca un veredicto.
"""
import importlib.util
import json
import os
import shutil
import sys
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(RAIZ, "scripts")
sys.path.insert(0, SCRIPTS)


def carga(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = carga("verifica_codigo", os.path.join(SCRIPTS, "verifica-codigo.py"))

TIENE_TSC = shutil.which("tsc") is not None
TIENE_NODE = shutil.which("node") is not None
TIENE_SQLITE = shutil.which("sqlite3") is not None


class TypeScriptRespetaElCompilador(unittest.TestCase):
    """El aprobado falso mas claro de la auditoria."""

    @unittest.skipUnless(TIENE_TSC and TIENE_NODE, "faltan tsc/node")
    def test_un_error_de_tipos_no_puede_pasar_las_pruebas(self):
        # Error real (TS2322) pero las aserciones en tiempo de ejecucion pasan:
        # ese es exactamente el caso que antes daba PASA LAS PRUEBAS.
        code = (
            "export function suma(a: number, b: number): number { return a + b; }\n"
            "const x: number = 'texto';\n"
        )
        pruebas = (
            "if (suma(2, 2) !== 4) { throw new Error('mal'); }\n"
            "console.log('PRUEBAS-OK');\n"
        )
        with V.Caja(sandbox=False) as c:
            estado, log = V.v_node(code, c, pruebas)
        self.assertEqual(estado, "NO COMPILA",
                         f"tsc rechaza esto; el veredicto fue {estado}: {log[:200]}")

    @unittest.skipUnless(TIENE_TSC and TIENE_NODE, "faltan tsc/node")
    def test_codigo_correcto_sigue_aprobando(self):
        code = "export function suma(a: number, b: number): number { return a + b; }\n"
        pruebas = ("if (suma(2, 2) !== 4) { throw new Error('mal'); }\n"
                   "console.log('PRUEBAS-OK');\n")
        with V.Caja(sandbox=False) as c:
            estado, log = V.v_node(code, c, pruebas)
        self.assertEqual(estado, "PASA LAS PRUEBAS", log[:300])

    @unittest.skipUnless(TIENE_TSC and TIENE_NODE, "faltan tsc/node")
    def test_compila_pero_falla_si_la_asercion_revienta(self):
        code = "export function suma(a: number, b: number): number { return a - b; }\n"
        pruebas = ("if (suma(2, 2) !== 4) { throw new Error('mal'); }\n"
                   "console.log('PRUEBAS-OK');\n")
        with V.Caja(sandbox=False) as c:
            estado, _ = V.v_node(code, c, pruebas)
        self.assertEqual(estado, "COMPILA PERO FALLA")


class SqlCompararFilasNoSubcadenas(unittest.TestCase):
    """El segundo aprobado falso: buscar numeros en la salida."""

    @unittest.skipUnless(TIENE_SQLITE, "falta sqlite3")
    def test_una_consulta_basura_con_los_numeros_no_aprueba(self):
        basura = "SELECT '2024 2025 2026 84000' AS basura;"
        with V.Caja(sandbox=False) as c:
            estado, log = V.v_sqlite(basura, c, None)
        self.assertNotEqual(estado, "PASA LAS PRUEBAS",
                            f"esta consulta no responde nada: {log[:200]}")

    @unittest.skipUnless(TIENE_SQLITE, "falta sqlite3")
    def test_la_consulta_de_referencia_aprueba(self):
        with V.Caja(sandbox=False) as c:
            estado, log = V.v_sqlite(V.C6_CONSULTA, c, None)
        self.assertEqual(estado, "PASA LAS PRUEBAS", log[:400])

    @unittest.skipUnless(TIENE_SQLITE, "falta sqlite3")
    def test_valores_mal_redondeados_no_aprueban(self):
        # Agregado equivocado (media en vez de suma): la forma de la salida es
        # la misma y los anios siguen apareciendo, que es lo que antes bastaba.
        mal = V.C6_CONSULTA.replace("SUM(bruto) AS bruto", "AVG(bruto) AS bruto")
        with V.Caja(sandbox=False) as c:
            estado, log = V.v_sqlite(mal, c, None)
        self.assertNotEqual(estado, "PASA LAS PRUEBAS", log[:300])

    @unittest.skipUnless(TIENE_SQLITE, "falta sqlite3")
    def test_compara_contra_el_esperado_del_caso_publico(self):
        """El esperado_filas de la bateria se compara ejecutando de verdad."""
        import json
        with open(os.path.join(RAIZ, "benchmarks", "bateria-publica.json")) as f:
            d = json.load(f)
        casos = d["prompts"] if isinstance(d, dict) else d
        caso = next(c for c in casos if c["id"] == "P-COD-SQL")
        with V.Caja(sandbox=False) as c:
            estado, log = V.v_sqlite(caso["_consulta_referencia"], c, None,
                                     esperado_filas=caso["esperado_filas"],
                                     esquema=caso["esquema"])
        self.assertEqual(estado, "PASA LAS PRUEBAS", log[:400])
        # Y una consulta que devuelve H2 ademas de H1 tiene que fallar:
        laxa = caso["_consulta_referencia"].replace("COUNT(*) > 2", "COUNT(*) > 1")
        with V.Caja(sandbox=False) as c:
            estado, _ = V.v_sqlite(laxa, c, None,
                                   esperado_filas=caso["esperado_filas"],
                                   esquema=caso["esquema"])
        self.assertEqual(estado, "COMPILA PERO FALLA")


class FalloDelBancoNoEsVeredicto(unittest.TestCase):
    """Hallazgo 6: un 203/EXEC se contaba como NO COMPILA."""

    def test_el_fallo_del_lanzador_levanta_ErrorBanco(self):
        """203/EXEC = systemd no llego a ejecutar el binario."""
        with V.Caja(sandbox=False) as c:
            original = V.subprocess.run

            class P:
                returncode = 203
                stdout = ""
                stderr = "Failed to execute /usr/bin/rustc: Permission denied"

            V.subprocess.run = lambda *a, **k: P()
            try:
                with self.assertRaises(V.ErrorBanco):
                    c.run(["rustc", "--version"])
            finally:
                V.subprocess.run = original

    def test_no_confunde_un_error_de_compilacion_con_fallo_del_banco(self):
        """Un compilador que dice 'error: ...' y sale 1 SI es veredicto."""
        with V.Caja(sandbox=False) as c:
            original = V.subprocess.run

            class P:
                returncode = 1
                stdout = ""
                stderr = "error[E0308]: mismatched types"

            V.subprocess.run = lambda *a, **k: P()
            try:
                rc, salida = c.run(["rustc", "a.rs"])
            finally:
                V.subprocess.run = original
        self.assertEqual(rc, 1)
        self.assertIn("E0308", salida)


class SandboxInconclusoNoCertifica(unittest.TestCase):
    """Hallazgo 6.2: si las comprobaciones no arrancan, no es 'aisla'."""

    def test_devuelve_None_si_las_comprobaciones_no_pudieron_correr(self):
        original = V.Caja.run

        def run_falso(self, cmd, segundos=60, stdin=None):
            if "/bin/echo" in cmd[0] or (len(cmd) > 1 and "vale" in str(cmd)):
                return 0, "vale"
            raise V.ErrorBanco("systemd-run no arranca")

        V.Caja.run = run_falso
        try:
            veredicto, motivo = V.comprueba_sandbox()
        finally:
            V.Caja.run = original
        self.assertIsNone(veredicto,
                          f"tenia que ser inconcluso y dijo {veredicto}: {motivo}")

    def test_un_sandbox_permisivo_se_detecta_como_no_aisla(self):
        original = V.Caja.run

        def run_falso(self, cmd, segundos=60, stdin=None):
            # Todo funciona, incluido el acceso a red: eso es NO aislar.
            return 0, "vale"

        V.Caja.run = run_falso
        try:
            veredicto, motivo = V.comprueba_sandbox()
        finally:
            V.Caja.run = original
        self.assertFalse(veredicto, motivo)


class CargadorDeBateriaSinDatosPrivados(unittest.TestCase):
    """Hallazgo 4: el modulo leia private/prompts.json al importarse."""

    def test_carga_la_bateria_publica_si_no_hay_privada(self):
        casos = V.carga_bateria(os.path.join(RAIZ, "benchmarks",
                                             "bateria-publica.json"))
        self.assertTrue(casos)
        self.assertIn("P-COD-SQL", casos)
        self.assertIn("p", casos["P-COD-SQL"])

    def test_falta_de_fichero_es_ErrorBanco_no_FileNotFoundError(self):
        with self.assertRaises(V.ErrorBanco):
            V.carga_bateria("/no/existe/bateria.json")

    def test_el_help_funciona_sin_datos_privados(self):
        """Un `--help` no puede depender de private/prompts.json."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            copia = os.path.join(d, "repo")
            shutil.copytree(RAIZ, copia,
                            ignore=shutil.ignore_patterns(".git", "private",
                                                          "__pycache__"))
            p = subprocess.run(
                [sys.executable, os.path.join(copia, "scripts", "verifica-codigo.py"),
                 "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[:400])
        self.assertIn("--bateria", p.stdout)




class FlujoCompletoDesdeCopiaLimpia(unittest.TestCase):
    """Hallazgo 4: 'enunciados publicos si, flujo reproducible todavia no'.

    Ejecuta el verificador de punta a punta en una copia del repo SIN
    `private/`, alimentandolo con la bateria publica y respuestas sinteticas.
    Es la prueba de que un tercero puede reproducir el flujo.
    """

    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        cls.repo = os.path.join(cls.tmp, "repo")
        shutil.copytree(RAIZ, cls.repo,
                        ignore=shutil.ignore_patterns(".git", "private",
                                                      "__pycache__", "*.pyc"))
        cls.subprocess = subprocess

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _corre(self, respuestas):
        import json
        bat = os.path.join(self.repo, "benchmarks", "bateria-publica.json")
        rp = os.path.join(self.tmp, "resp.json")
        with open(rp, "w") as f:
            json.dump(respuestas, f)
        p = self.subprocess.run(
            [sys.executable, os.path.join(self.repo, "scripts", "verifica-codigo.py"),
             "--etiqueta", "prueba", "--bateria", bat, "--respuestas", rp,
             "--sin-sandbox", "--solo", ",".join(respuestas),
             "--salida", os.path.join(self.tmp, "out.json")],
            capture_output=True, text=True, timeout=300)
        return p

    @unittest.skipUnless(TIENE_SQLITE, "falta sqlite3")
    def test_la_consulta_correcta_aprueba_sin_datos_privados(self):
        import json
        with open(os.path.join(self.repo, "benchmarks", "bateria-publica.json")) as f:
            d = json.load(f)
        casos = d["prompts"] if isinstance(d, dict) else d
        caso = next(c for c in casos if c["id"] == "P-COD-SQL")
        resp = {"P-COD-SQL": {"texto": "```sql\n" + caso["_consulta_referencia"] + "\n```"}}
        p = self._corre(resp)
        self.assertIn("PASA LAS PRUEBAS", p.stdout + p.stderr,
                      f"stdout={p.stdout[-600:]}\nstderr={p.stderr[-600:]}")
        self.assertEqual(p.returncode, 0, p.stderr[-400:])

    @unittest.skipUnless(TIENE_SQLITE, "falta sqlite3")
    def test_la_consulta_basura_no_aprueba_y_el_codigo_de_salida_lo_dice(self):
        resp = {"P-COD-SQL": {"texto": "```sql\nSELECT '2024 2025 2026 84000';\n```"}}
        p = self._corre(resp)
        self.assertNotIn("[OK]", p.stdout)
        self.assertEqual(p.returncode, 2,
                         f"fallo del MODELO = 2, no 0: {p.stdout[-500:]}")


class MarcadorDelProtocoloCoherente(unittest.TestCase):
    """H-026: la bateria publica terminaba sus pruebas con print('ok') mientras
    v_python/v_node exigian PRUEBAS-OK -> una solucion CORRECTA se puntuaba
    'COMPILA PERO FALLA'. Penalizar codigo bueno por un desajuste del propio
    protocolo falsea cualquier comparacion entre modelos.

    No se elimina la exigencia del marcador: un exit 0 no demuestra por si solo
    que las aserciones llegaran a ejecutarse (un `return` temprano, un bloque
    saltado o un proceso que muere limpio tambien salen 0).
    """

    def _casos(self, ruta):
        with open(ruta, encoding="utf-8") as fh:
            d = json.load(fh)
        ps = d["prompts"] if isinstance(d, dict) else d
        return [p for p in ps if isinstance(p, dict) and (p.get("pruebas") or "")]

    def test_toda_prueba_de_la_bateria_publica_emite_el_marcador(self):
        casos = self._casos(os.path.join(RAIZ, "benchmarks", "bateria-publica.json"))
        self.assertTrue(casos, "la bateria publica deberia traer casos con pruebas")
        for p in casos:
            with self.subTest(id=p.get("id")):
                self.assertIn("PRUEBAS-OK", p["pruebas"],
                              "las pruebas deben emitir el marcador que exige el verificador")

    def test_la_tabla_de_pruebas_heredada_tambien_lo_emite(self):
        mod = V
        for pid, (lang, pruebas) in mod.PRUEBAS.items():
            with self.subTest(id=pid):
                self.assertIn("PRUEBAS-OK", pruebas)

    def test_el_marcador_sigue_siendo_obligatorio(self):
        """Control negativo: exit 0 sin marcador NO es un pase."""
        with V.Caja(sandbox=False) as caja:
            estado, _ = V.v_python("def f():\n    return 1\n", caja,
                                   "assert f() == 1\nprint('ok')")
        self.assertNotEqual(estado, "PASA LAS PRUEBAS",
                            "sin el marcador no se puede acreditar que las aserciones corrieran")


SOL_PY_BUENA = '''def media_movil(datos, k):
    if k <= 0 or k > len(datos):
        return []
    out = []
    s = sum(datos[:k])
    out.append(s / k)
    for i in range(k, len(datos)):
        s += datos[i] - datos[i - k]
        out.append(s / k)
    return out
'''

SOL_PY_MALA = '''def media_movil(datos, k):
    return [sum(datos[i:i + k]) / k for i in range(len(datos))]
'''

SOL_TS_BUENA = '''export function agrupaPor<T, K extends string>(xs: T[], f: (x: T) => K): Record<K, T[]> {
  const out = {} as Record<K, T[]>;
  for (const x of xs) {
    const k = f(x);
    if (!out[k]) out[k] = [];
    out[k].push(x);
  }
  return out;
}
'''

SOL_TS_MALA = '''export function agrupaPor<T, K extends string>(xs: T[], f: (x: T) => K): Record<K, T[]> {
  const out = {} as Record<K, T[]>;
  for (const x of xs) out[f(x)] = [x];
  return out;
}
'''

SOL_SQL_BUENA = ("SELECT horno, ROUND(AVG(grados),1) AS media FROM lecturas "
                 "GROUP BY horno HAVING COUNT(*) > 2 ORDER BY media DESC;")
# El fallo tipico: >= 2 en vez de > 2, que cuela H2.
SOL_SQL_MALA = ("SELECT horno, ROUND(AVG(grados),1) AS media FROM lecturas "
                "GROUP BY horno HAVING COUNT(*) >= 2 ORDER BY media DESC;")


class CadaLenguajePublicoPuedeAprobarYSuspender(unittest.TestCase):
    """H-026: la prueba que faltaba. La copia limpia solo cubria SQL, asi que el
    falso negativo de Python y TypeScript pasaba desapercibido. Un banco sirve
    solo si la solucion buena aprueba Y la mala suspende: comprobar una sola de
    las dos mitades deja pasar tanto los falsos negativos como los aprobados
    falsos.
    """

    RUTA = os.path.join(RAIZ, "benchmarks", "bateria-publica.json")

    def _ejecuta(self, caso, codigo, lenguaje):
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        resp = os.path.join(d, "r.json")
        with open(resp, "w", encoding="utf-8") as fh:
            json.dump({caso: {"texto": "```%s\n%s```" % (lenguaje, codigo)}}, fh)
        salida = os.path.join(d, "s.json")
        p = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "verifica-codigo.py"),
             "--etiqueta", "prueba", "--bateria", self.RUTA,
             "--respuestas", resp, "--salida", salida,
             "--solo", caso, "--sin-sandbox"],
            capture_output=True, text=True, timeout=300)
        with open(salida, encoding="utf-8") as fh:
            datos = json.load(fh)
        res = datos[caso] if caso in datos else datos
        return res.get("veredicto"), p.returncode

    def test_python_bueno_aprueba_y_malo_suspende(self):
        estado, rc = self._ejecuta("P-COD-PY", SOL_PY_BUENA, "python")
        self.assertEqual(estado, "PASA LAS PRUEBAS",
                         "una solucion correcta NO puede puntuarse como fallo")
        self.assertEqual(rc, 0)
        estado, rc = self._ejecuta("P-COD-PY", SOL_PY_MALA, "python")
        self.assertEqual(estado, "COMPILA PERO FALLA")
        self.assertNotEqual(rc, 0)

    @unittest.skipUnless(TIENE_TSC and TIENE_NODE, "faltan tsc/node")
    def test_typescript_bueno_aprueba_y_malo_suspende(self):
        estado, rc = self._ejecuta("P-COD-TS", SOL_TS_BUENA, "typescript")
        self.assertEqual(estado, "PASA LAS PRUEBAS",
                         "una solucion correcta NO puede puntuarse como fallo")
        self.assertEqual(rc, 0)
        estado, _ = self._ejecuta("P-COD-TS", SOL_TS_MALA, "typescript")
        self.assertEqual(estado, "COMPILA PERO FALLA")

    @unittest.skipUnless(TIENE_SQLITE, "falta sqlite3")
    def test_sql_bueno_aprueba_y_malo_suspende(self):
        estado, rc = self._ejecuta("P-COD-SQL", SOL_SQL_BUENA, "sql")
        self.assertEqual(estado, "PASA LAS PRUEBAS")
        self.assertEqual(rc, 0)
        estado, _ = self._ejecuta("P-COD-SQL", SOL_SQL_MALA, "sql")
        self.assertEqual(estado, "COMPILA PERO FALLA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
