#!/usr/bin/env python3
"""Pruebas del lote P0: los seis fallos de ingenieria que la auditoria externa
encontro en la campana H-031 (`scripts/campana-nocturna.py`).

Cada clase reproduce UN fallo concreto y fija el comportamiento corregido. Ni
GPU, ni modelo, ni M5: un servidor HTTP de mentira, un `systemctl` de mentira
que es un script de bash con un fichero de estado, y el sistema de ficheros en
un tmpdir. Los scripts reales se ejecutan COMO PROCESOS, que es como se
ejecutan de verdad.

    python3 tests/test_p0.py -v

Criterio de aceptacion (docs/metodologia.md, regla 8): toda prueba de aqui
FALLA contra el commit 84d20b2, el anterior al arreglo.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(RAIZ, "scripts")
sys.path.insert(0, SCRIPTS)

from validacion import ErrorInfraestructura  # noqa: E402


def modulo(nombre):
    """Import PEREZOSO de los modulos nuevos de este lote.

    No va en la cabecera a proposito. Con `import salud` arriba, la bateria
    entera muere de ImportError contra el commit anterior al arreglo y las 35
    pruebas "fallan" por el mismo motivo, que no demuestra gran cosa.
    Cargando cada modulo donde se usa, las pruebas que no dependen de un
    fichero nuevo -- el gate exacto, la credencial, el rollback -- fallan alli
    por lo que tienen que fallar.
    """
    return importlib.import_module(nombre)


CLAVE_BUENA = "clave-de-pruebas-vigente-0001"
CLAVE_VIEJA = "clave-de-pruebas-rotada-9999"
MODELO = "modelo-de-pruebas"


def carga(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# El M5 de mentira: systemctl de bash con fichero de estado + servidor HTTP
# ===========================================================================
UNIDAD = "llama-pruebas"

SYSTEMCTL_FALSO = r"""#!/usr/bin/env bash
# systemctl de mentira para las pruebas de P0.
BASE="__BASE__"
UNIT="$BASE/systemd/__UNIDAD__.service"
EST="$BASE/estado"
case "${1:-}" in
  cat)            cat "$UNIT" ;;
  is-active)      cat "$EST" ;;
  is-enabled)     echo enabled ;;
  start|restart)  echo active > "$EST" ;;
  stop)           echo inactive > "$EST" ;;
  daemon-reload)  : ;;
  list-unit-files) : ;;
  show)           cat "$EST" ;;
  *)              : ;;
esac
exit 0
"""

UNIDAD_FALSA = """[Unit]
Description=llama.cpp server - pruebas

[Service]
ExecStart=/models/llama.cpp/build/bin/llama-server \\
  --host 0.0.0.0 --port {puerto} \\
  --api-key-file {keyfile} \\
  --batch-size 4096 --ubatch-size 1024 \\
  --cache-ram 4096
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


class M5Falso:
    """Directorio con unidad, fichero de claves, .env residual y systemctl."""

    def __init__(self, puerto=8080, clave_keyfile=CLAVE_BUENA,
                 clave_env=CLAVE_VIEJA):
        self.base = tempfile.mkdtemp(prefix="p0-m5-")
        os.makedirs(os.path.join(self.base, "systemd"))
        os.makedirs(os.path.join(self.base, "bin"))
        self.keyfile = os.path.join(self.base, "api-keys.txt")
        with open(self.keyfile, "w") as f:
            f.write(clave_keyfile + "\n")
        # El .env residual que la campana vieja leia por error (fallo A).
        with open(os.path.join(self.base, "llama-server.env"), "w") as f:
            f.write(f"LLAMA_API_KEY={clave_env}\n")
        self.ruta_unidad = os.path.join(self.base, "systemd", f"{UNIDAD}.service")
        with open(self.ruta_unidad, "w") as f:
            f.write(UNIDAD_FALSA.format(puerto=puerto, keyfile=self.keyfile))
        self.estado = os.path.join(self.base, "estado")
        with open(self.estado, "w") as f:
            f.write("active\n")
        self.systemctl = os.path.join(self.base, "bin", "systemctl")
        with open(self.systemctl, "w") as f:
            f.write(SYSTEMCTL_FALSO.replace("__BASE__", self.base)
                    .replace("__UNIDAD__", UNIDAD))
        os.chmod(self.systemctl, os.stat(self.systemctl).st_mode | stat.S_IEXEC)

    def pon_estado(self, v):
        with open(self.estado, "w") as f:
            f.write(v + "\n")

    def lee_estado(self):
        with open(self.estado) as f:
            return f.read().strip()

    def texto_unidad(self):
        with open(self.ruta_unidad) as f:
            return f.read()

    def entorno(self, **extra):
        e = dict(os.environ, SYSTEMCTL=self.systemctl, SUDO="",
                 LLAMA_UNIT_DIR=os.path.join(self.base, "systemd"))
        e.update(extra)
        return e

    def limpia(self):
        shutil.rmtree(self.base, ignore_errors=True)


# Estado compartido del servidor HTTP falso.
GUION = {"clave": CLAVE_BUENA, "respuesta": "391", "ruta_unidad": None,
         "marca_roja": None, "ultimo_cuerpo": None}


class ServidorFalso(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _envia(self, codigo, obj):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        return self.headers.get("Authorization") == f"Bearer {GUION['clave']}"

    def do_GET(self):
        if self.path == "/health":
            return self._envia(200, {"status": "ok"})
        if not self._auth():
            return self._envia(401, {"error": {"message": "clave invalida"}})
        if self.path == "/v1/models":
            return self._envia(200, {"data": [{"id": MODELO}]})
        return self._envia(404, {})

    def do_POST(self):
        if not self._auth():
            return self._envia(401, {"error": {"message": "clave invalida"}})
        n = int(self.headers.get("Content-Length", 0))
        GUION["ultimo_cuerpo"] = self.rfile.read(n).decode("utf-8", "replace")
        # Un servidor que se porta distinto segun la configuracion que systemd
        # tiene escrita: asi la campana ve rojo con la unidad modificada y
        # verde cuando el rollback la devuelve a su sitio.
        contenido = GUION["respuesta"]
        ru, marca = GUION["ruta_unidad"], GUION["marca_roja"]
        if ru and marca and os.path.exists(ru):
            with open(ru) as f:
                if marca in f.read():
                    contenido = "592"
        return self._envia(200, {
            "choices": [{"message": {"content": contenido},
                         "finish_reason": "stop"}],
            "timings": {"prompt_n": 12, "prompt_per_second": 120.5,
                        "predicted_per_second": 25.4, "predicted_n": 3}})


class ConServidor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ServidorFalso)
        cls.puerto = cls.srv.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.puerto}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        GUION.update({"clave": CLAVE_BUENA, "respuesta": "391",
                      "ruta_unidad": None, "marca_roja": None,
                      "ultimo_cuerpo": None})
        self.m5 = M5Falso(puerto=self.puerto)
        self.addCleanup(self.m5.limpia)


# ===========================================================================
# A. La credencial sale del ExecStart efectivo, no de un .env de al lado
# ===========================================================================
class CredencialDelExecStart(ConServidor):
    """Fallo A: restauracion.sh leia LLAMA_API_KEY de /etc/llama-server/*.env
    mientras la unidad arrancaba con --api-key-file. Coincidian por casualidad
    (un .env residual). El dia que se rote una y no la otra, el gate miente en
    la direccion que toque: falso rojo con produccion sana, o falso VERDE con
    el servidor sirviendo otra clave."""

    def credencial(self, *args):
        return subprocess.run(
            ["bash", os.path.join(SCRIPTS, "credencial.sh"), *args],
            capture_output=True, text=True, timeout=60, env=self.m5.entorno())

    def restauracion(self):
        return subprocess.run(
            ["bash", os.path.join(SCRIPTS, "restauracion.sh"), UNIDAD,
             str(self.puerto), MODELO],
            capture_output=True, text=True, timeout=180, env=self.m5.entorno())

    def test_deriva_la_clave_del_api_key_file_del_execstart(self):
        p = self.credencial("--crudo", UNIDAD)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), CLAVE_BUENA)

    def test_deriva_la_clave_de_api_key_en_linea(self):
        with open(self.m5.ruta_unidad, "w") as f:
            f.write("[Service]\nExecStart=-/bin/llama-server "
                    "--api-key clave-en-la-linea --port 8080\n")
        p = self.credencial("--crudo", UNIDAD)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "clave-en-la-linea")

    def test_falla_si_el_execstart_no_declara_credencial(self):
        with open(self.m5.ruta_unidad, "w") as f:
            f.write("[Service]\nExecStart=/bin/llama-server --port 8080\n")
        p = self.credencial("--crudo", UNIDAD)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--api-key", p.stderr)

    def test_sin_crudo_no_ensena_la_clave(self):
        p = self.credencial(UNIDAD)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(CLAVE_BUENA, p.stdout + p.stderr)
        self.assertIn("...", p.stdout)

    # -- las dos obligatorias del brief ----------------------------------
    def test_clave_buena_en_api_keys_y_vieja_en_env_da_VERDE(self):
        """El caso que hoy pasa por casualidad tiene que pasar por construccion."""
        p = self.restauracion()
        self.assertEqual(p.returncode, 0,
                         f"produccion sana y el gate dio rojo\n{p.stdout}\n{p.stderr}")
        self.assertIn("credencial derivada del ExecStart", p.stdout)
        self.assertNotIn(CLAVE_BUENA, p.stdout + p.stderr)

    def test_clave_vieja_en_api_keys_y_buena_en_env_da_ROJO(self):
        """El caso invertido: la fuente correcta es la del ExecStart, y con una
        clave caducada ahi el gate tiene que ponerse rojo aunque el .env de al
        lado traiga la buena. Leer el .env daria un falso verde."""
        with open(self.m5.keyfile, "w") as f:
            f.write(CLAVE_VIEJA + "\n")
        with open(os.path.join(self.m5.base, "llama-server.env"), "w") as f:
            f.write(f"LLAMA_API_KEY={CLAVE_BUENA}\n")
        p = self.restauracion()
        self.assertNotEqual(p.returncode, 0,
                            f"el gate dio verde con la clave caducada\n{p.stdout}")
        self.assertIn("credencial derivada del ExecStart", p.stdout)
        self.assertNotIn(CLAVE_BUENA, p.stdout + p.stderr)


# ===========================================================================
# B. Una sola espera: unidad activa AND /health AND modelo correcto
# ===========================================================================
class EsperaUnicaDeServicio(ConServidor):
    """Fallo B: campana-nocturna.py::espera_prod devolvia True con /health=200
    y consultaba is-active DESPUES, asi que cualquier proceso escuchando en el
    puerto daba verde a 'produccion restaurada'."""

    def _sh(self, estado):
        return lambda cmd: estado

    @staticmethod
    def espera_servicio(*a, **k):
        return modulo("salud").espera_servicio(*a, **k)

    def test_puerto_con_200_pero_unidad_inactiva_no_es_verde(self):
        self.assertFalse(
            self.espera_servicio(UNIDAD, self.puerto, modelo=MODELO, limite=2,
                            clave=CLAVE_BUENA, sh=self._sh("inactive"),
                            pausa=0.1, traza=lambda *a: None))

    def test_unidad_failed_lanza_error_de_infraestructura(self):
        with self.assertRaises(ErrorInfraestructura):
            self.espera_servicio(UNIDAD, self.puerto, modelo=MODELO, limite=30,
                            clave=CLAVE_BUENA, sh=self._sh("failed"),
                            pausa=0.1, traza=lambda *a: None)

    def test_unidad_activa_y_modelo_correcto_es_verde(self):
        self.assertTrue(
            self.espera_servicio(UNIDAD, self.puerto, modelo=MODELO, limite=5,
                            clave=CLAVE_BUENA, sh=self._sh("active"),
                            pausa=0.1, traza=lambda *a: None))

    def test_unidad_activa_sirviendo_OTRO_modelo_no_es_verde(self):
        """Lo que (1)+(2) siguen aceptando: la unidad sana con el GGUF que no es."""
        self.assertFalse(
            self.espera_servicio(UNIDAD, self.puerto, modelo="otro-modelo", limite=2,
                            clave=CLAVE_BUENA, sh=self._sh("active"),
                            pausa=0.1, traza=lambda *a: None))

    def test_sin_la_clave_no_puede_acreditar_el_modelo(self):
        self.assertFalse(
            self.espera_servicio(UNIDAD, self.puerto, modelo=MODELO, limite=2,
                            clave="clave-mala", sh=self._sh("active"),
                            pausa=0.1, traza=lambda *a: None))

    def test_el_barrido_usa_la_espera_compartida_y_no_la_suya(self):
        bench = carga("bench_ubatch_p0", os.path.join(SCRIPTS, "bench-ubatch.py"))
        with open(os.path.join(SCRIPTS, "bench-ubatch.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("salud.espera_servicio", src,
                      "bench-ubatch.py debe delegar en la espera unica")
        self.assertTrue(callable(bench.espera_salud))


# ===========================================================================
# C. El gate exacto: 391, y con el pensamiento desactivado
# ===========================================================================
class SmokeExactoConPensamientoDesactivado(ConServidor):
    """Fallo C: el gate de promocion de la campana aceptaba 'contenido no vacio
    + finish_reason=stop', que aprueba un backend que responde MAL. El gate es
    smoke-test.sh (exacto), y le faltaba fijar enable_thinking=false: el unico
    script del banco que seguia expuesto al bucle de razonamiento de H-019."""

    def corre(self):
        return subprocess.run(
            ["bash", os.path.join(SCRIPTS, "smoke-test.sh"), self.url],
            capture_output=True, text=True, timeout=180,
            env=dict(os.environ, LLAMA_API_KEY=CLAVE_BUENA))

    def test_391_es_verde_y_la_peticion_desactiva_el_pensamiento(self):
        p = self.corre()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cuerpo = json.loads(GUION["ultimo_cuerpo"])
        self.assertEqual(cuerpo.get("chat_template_kwargs"),
                         {"enable_thinking": False},
                         "la peticion del gate exacto tiene que desactivar el "
                         "pensamiento, como el resto del instrumental")

    def test_592_es_rojo_y_la_peticion_desactiva_el_pensamiento(self):
        GUION["respuesta"] = "592"
        p = self.corre()
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertIn("no es exactamente 391", p.stdout)
        cuerpo = json.loads(GUION["ultimo_cuerpo"])
        self.assertEqual(cuerpo.get("chat_template_kwargs"),
                         {"enable_thinking": False})


# ===========================================================================
# D. Rollback: restaurar la unidad NO es restaurar la build
# ===========================================================================
class BuildsVersionadasYSymlink(unittest.TestCase):
    """El mecanismo por si solo, sin root: builds.sh acepta LLAMA_BUILDS_DIR y
    LLAMA_CURRENT por entorno justamente para poder probarlo aqui."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="p0-builds-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.dir_builds = os.path.join(self.base, "llama-builds")
        self.actual = os.path.join(self.base, "llama-current")
        for sha in ("aaaa1111", "bbbb2222"):
            os.makedirs(os.path.join(self.dir_builds, sha, "build", "bin"))

    def builds(self, *args):
        return subprocess.run(
            ["bash", os.path.join(SCRIPTS, "builds.sh"), *args],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, LLAMA_BUILDS_DIR=self.dir_builds,
                     LLAMA_CURRENT=self.actual))

    def apunta_a(self):
        return os.path.basename(os.path.realpath(self.actual))

    def test_promover_mueve_el_symlink_y_registra_el_anterior(self):
        self.assertEqual(self.builds("promover", "aaaa1111").returncode, 0)
        self.assertEqual(self.apunta_a(), "aaaa1111")
        self.assertEqual(self.builds("promover", "bbbb2222").returncode, 0)
        self.assertEqual(self.apunta_a(), "bbbb2222")
        with open(os.path.join(self.dir_builds, "ANTERIOR")) as f:
            self.assertEqual(f.read().strip(), "aaaa1111")

    def test_volver_deshace_la_promocion(self):
        self.builds("promover", "aaaa1111")
        self.builds("promover", "bbbb2222")
        self.assertEqual(self.builds("volver").returncode, 0)
        self.assertEqual(self.apunta_a(), "aaaa1111")

    def test_promover_no_toca_ninguna_unidad(self):
        """La migracion del ExecStart al symlink es otra ventana autorizada:
        builds.sh solo prepara el mecanismo y no puede hablar con systemd."""
        p = self.builds("promover", "aaaa1111")
        self.assertIn("NO se ha tocado", p.stdout)
        with open(os.path.join(SCRIPTS, "builds.sh"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("systemctl", src,
                         "builds.sh no puede invocar a systemd")

    def test_volver_sin_anterior_registrado_falla(self):
        p = self.builds("volver")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nada que deshacer", p.stderr)


class CampanaBase(ConServidor):
    """Monta ademas el arbol de builds y deja lista la invocacion del runner."""

    def setUp(self):
        super().setUp()
        self.dir_builds = os.path.join(self.m5.base, "llama-builds")
        self.actual = os.path.join(self.m5.base, "llama-current")
        for sha in ("base0000", "cand1111"):
            os.makedirs(os.path.join(self.dir_builds, sha, "build", "bin"))
        subprocess.run(["bash", os.path.join(SCRIPTS, "builds.sh"), "promover",
                        "base0000"], capture_output=True, text=True,
                       env=self._entorno())
        self.salida = os.path.join(self.m5.base, "salida")

    def _entorno(self, **extra):
        return self.m5.entorno(LLAMA_BUILDS_DIR=self.dir_builds,
                               LLAMA_CURRENT=self.actual, **extra)

    def corre_campana(self, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "campana.py"),
             "--run-id", "p0-prueba", "--unidad", UNIDAD, "--modelo", MODELO,
             "--puerto-prod", str(self.puerto), "--puerto-banco", "8099",
             "--baseline-sha", "base0000", "--candidato-sha", "cand1111",
             "--saltar-construccion", "--salida", self.salida, *extra],
            capture_output=True, text=True, timeout=600, env=self._entorno())

    def apunta_a(self):
        return os.path.basename(os.path.realpath(self.actual))

    def resultados(self):
        with open(os.path.join(self.salida, "resultados.json")) as f:
            return json.load(f)


class RollbackDeUnidadYDeBuild(CampanaBase):
    """Fallo D: la campana compilo ENCIMA de /models/llama.cpp y, al fallar el
    smoke, restauro solo la unidad. La unidad restaurada seguia apuntando al
    mismo path, ahora con el binario nuevo dentro: el rollback no revertia
    nada. Aqui las dos cosas se deshacen, y se comprueban las dos."""

    def setUp(self):
        super().setUp()
        # El servidor responde 592 mientras la unidad lleve el cambio aplicado.
        GUION["ruta_unidad"] = self.m5.ruta_unidad
        GUION["marca_roja"] = "--cache-ram 12288"

    def test_gate_rojo_revierte_symlink_y_unidad(self):
        original = self.m5.texto_unidad()
        p = self.corre_campana("--forzar-adopcion", "--cambio-unidad",
                               "--cache-ram 4096=>--cache-ram 12288")
        self.assertNotEqual(p.returncode, 0,
                            f"gate rojo y salio 0\n{p.stdout[-2000:]}")
        self.assertEqual(self.apunta_a(), "base0000",
                         "el symlink de build no volvio al anterior")
        self.assertEqual(self.m5.texto_unidad(), original,
                         "la unidad no se restauro desde el backup")
        r = self.resultados()
        self.assertFalse(r["aplicado"])
        self.assertTrue(r["rollback"]["build_revertida"])
        self.assertTrue(r["rollback"]["produccion_sana"])
        self.assertFalse(r["gates"]["tras-aplicar"]["smoke_exacto"])
        self.assertTrue(r["gates"]["tras-rollback"]["smoke_exacto"])

    def test_el_codigo_de_salida_del_runner_no_es_cero_con_gate_rojo(self):
        p = self.corre_campana("--forzar-adopcion", "--cambio-unidad",
                               "--cache-ram 4096=>--cache-ram 12288")
        self.assertEqual(p.returncode, 3, p.stdout[-2000:])

    def test_gate_verde_deja_el_cambio_y_la_build_puestos(self):
        GUION["marca_roja"] = "no-aparece-en-la-unidad"
        p = self.corre_campana("--forzar-adopcion", "--cambio-unidad",
                               "--cache-ram 4096=>--cache-ram 12288")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        self.assertEqual(self.apunta_a(), "cand1111")
        self.assertIn("--cache-ram 12288", self.m5.texto_unidad())

    def test_un_cambio_de_unidad_que_no_casa_es_un_error(self):
        """La campana vieja tenia dos ramas para --cache-ram y ninguna
        comprobaba que el texto hubiera cambiado de verdad."""
        # Forma pegada (--opcion=valor): un valor que empieza por '-' y no
        # lleva espacios, argparse lo tomaria por otra opcion.
        p = self.corre_campana("--forzar-adopcion",
                               "--cambio-unidad=--no-existe-esto=>--otro")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no aparece en la unidad", p.stdout)

    def test_la_clave_no_aparece_en_la_salida_ni_en_el_json(self):
        self.corre_campana("--forzar-adopcion")
        p = self.corre_campana("--forzar-adopcion")
        with open(os.path.join(self.salida, "resultados.json")) as f:
            crudo = f.read()
        with open(os.path.join(self.salida, "campana.log")) as f:
            log = f.read()
        for texto in (p.stdout, p.stderr, crudo, log):
            self.assertNotIn(CLAVE_BUENA, texto)


class EstadoInicialRespetado(CampanaBase):
    """Fallo E: la campana vieja arrancaba produccion SIEMPRE al final, en un
    `finally`, aunque la hubiera encontrado parada. Eso no es restaurar: es
    cambiar el estado de la maquina por su cuenta."""

    def test_produccion_parada_al_empezar_sigue_parada_al_acabar(self):
        self.m5.pon_estado("inactive")
        p = self.corre_campana("--forzar-adopcion")
        self.assertEqual(p.returncode, 0, p.stdout[-1500:])
        self.assertEqual(self.m5.lee_estado(), "inactive",
                         "la campana arranco una produccion que estaba parada")

    def test_parada_al_empezar_no_promueve_ni_toca_la_unidad(self):
        """Sin produccion en marcha no hay gate, y sin gate no se aplica nada."""
        self.m5.pon_estado("inactive")
        original = self.m5.texto_unidad()
        self.corre_campana("--forzar-adopcion", "--cambio-unidad",
                           "--cache-ram 4096=>--cache-ram 12288")
        self.assertEqual(self.apunta_a(), "base0000")
        self.assertEqual(self.m5.texto_unidad(), original)
        r = self.resultados()
        self.assertEqual(r["estado_inicial"], "inactive")
        self.assertFalse(r["aplicado"])

    def test_produccion_activa_al_empezar_queda_activa(self):
        p = self.corre_campana("--forzar-adopcion")
        self.assertEqual(p.returncode, 0, p.stdout[-1500:])
        self.assertEqual(self.m5.lee_estado(), "active")

    def test_el_backup_lleva_el_run_id_y_no_una_fecha_cableada(self):
        self.corre_campana("--forzar-adopcion")
        self.assertTrue(os.path.exists(self.m5.ruta_unidad + ".bak-p0-prueba"))


# ===========================================================================
# F. Corpus congelado y medido con el tokenizador real
# ===========================================================================
class CorpusCongelado(unittest.TestCase):
    """Fallo F: TOKENS_POR_PALABRA=1.78 estimaba un 24 % corto (2.294 reales
    frente a 3.000 pedidos) y las etiquetas '3k/24k' de las tablas eran
    falsas. Ademas el texto se regeneraba en cada ejecucion, asi que nada
    garantizaba que el brazo A y el brazo B midieran el mismo prompt."""

    def setUp(self):
        self.p = modulo("prompts")
        self.dir = tempfile.mkdtemp(prefix="p0-corpus-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def medidor(self, factor=2.1):
        return lambda t: int(len(t.split()) * factor)

    def congela(self, objetivo=2048):
        texto, pn = self.p.genera(objetivo, self.medidor(), traza=lambda *a: None)
        return self.p.congela(objetivo, texto, pn, self.dir)

    def test_el_corpus_cae_dentro_de_la_tolerancia_medida(self):
        for objetivo in (2048, 8192, 65536):
            with self.subTest(objetivo=objetivo):
                _, pn = self.p.genera(objetivo, self.medidor(),
                                      traza=lambda *a: None)
                self.assertLessEqual(abs(pn - objetivo), self.p.TOLERANCIA)

    def test_fuera_de_tolerancia_aborta(self):
        with self.assertRaises(self.p.ErrorCorpus) as c:
            self.p.verifica_prompt_n(8192, 8192 + self.p.TOLERANCIA + 1)
        self.assertIn("tokenizador", str(c.exception))

    def test_dentro_de_tolerancia_no_aborta(self):
        self.assertEqual(
            self.p.verifica_prompt_n(8192, 8192 - self.p.TOLERANCIA),
            8192 - self.p.TOLERANCIA)

    def test_mismo_fichero_y_mismo_hash_para_los_dos_brazos(self):
        e = self.congela(2048)
        brazo_a = self.p.carga(2048, self.dir)
        brazo_b = self.p.carga(2048, self.dir)
        self.assertEqual(brazo_a["ruta"], brazo_b["ruta"])
        self.assertEqual(brazo_a["sha256"], brazo_b["sha256"])
        self.assertEqual(brazo_a["texto"], brazo_b["texto"])
        self.assertEqual(brazo_a["sha256"], e["sha256"])
        self.assertEqual(
            hashlib.sha256(brazo_a["texto"].encode()).hexdigest(), e["sha256"])

    def test_el_texto_es_determinista(self):
        self.assertEqual(self.p.texto_de(120), self.p.texto_de(120))
        self.assertNotEqual(self.p.texto_de(120), self.p.texto_de(121))

    def test_un_corpus_editado_a_mano_no_se_da_por_bueno(self):
        e = self.congela(2048)
        with open(os.path.join(self.dir, e["fichero"]), "a") as f:
            f.write(" coletilla anadida a mano")
        with self.assertRaises(self.p.ErrorCorpus) as c:
            self.p.carga(2048, self.dir)
        self.assertIn("sha256", str(c.exception))

    def test_sin_corpus_congelado_avisa_en_vez_de_inventarlo(self):
        with self.assertRaises(self.p.ErrorCorpus) as c:
            self.p.carga(32768, self.dir)
        self.assertIn("generalo", str(c.exception))

    def test_el_manifiesto_declara_sha256_objetivo_y_prompt_n(self):
        self.congela(2048)
        man = self.p.lee_manifiesto(self.dir)
        e = man["corpus"]["2048"]
        for campo in ("fichero", "sha256", "objetivo", "prompt_n", "fecha",
                      "medido_con"):
            self.assertIn(campo, e)
        self.assertEqual(len(e["sha256"]), 64)
        self.assertEqual(self.p.verifica_ficheros(self.dir), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
