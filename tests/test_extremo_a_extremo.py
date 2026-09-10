#!/usr/bin/env python3
# Pruebas de EXTREMO A EXTREMO: se levanta un servidor HTTP falso que imita a
# llama-server y se ejecutan los scripts REALES como procesos, comprobando el
# CODIGO DE SALIDA.
#
# La auditoria externa senalo que las pruebas unitarias de los validadores no
# demuestran que los scripts fallen: salian 0 pasara lo que pasara. Aqui se
# comprueba el proceso entero.
#
#   python3 tests/test_extremo_a_extremo.py

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(RAIZ, "scripts")
CLAVE = "clave-de-pruebas-no-secreta"

# Modo de respuesta del servidor falso. Lo cambia cada prueba.
MODO = {"v": "normal"}


class Falso(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        pass  # sin ruido en la salida de las pruebas

    def _envia(self, codigo, obj):
        cuerpo = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _auth_ok(self):
        """El servidor falso valida la clave de verdad: asi el smoke test
        puede comprobar que la autenticacion RECHAZA lo que debe."""
        if MODO["v"] == "sin-auth":
            return True  # simula un servidor abierto: el smoke test debe quejarse
        return self.headers.get("Authorization") == f"Bearer {CLAVE}"

    def do_GET(self):
        if not self._auth_ok():
            return self._envia(401, {"error": {"message": "clave invalida"}})
        if self.path == "/health":
            if MODO["v"] == "salud-mala":
                return self._envia(503, {"status": "loading model"})
            return self._envia(200, {"status": "ok"})
        if self.path == "/v1/models":
            if MODO["v"] == "salud-mala":
                return self._envia(503, {})
            return self._envia(200, {"data": [{"id": "modelo-de-pruebas"}]})
        return self._envia(404, {})

    def do_POST(self):
        if not self._auth_ok():
            return self._envia(401, {"error": {"message": "clave invalida"}})
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        m = MODO["v"]

        if m == "json-vacio":
            # el caso original: {} se tomaba como medida valida de 0 t/s
            return self._envia(200, {})
        if m == "sin-timings":
            return self._envia(200, {
                "choices": [{"message": {"content": "391"}, "finish_reason": "stop"}]})
        if m == "contenido-vacio":
            return self._envia(200, {
                "choices": [{"message": {"content": "", "reasoning_content": "pienso"}},
                            ],
                "timings": {"prompt_n": 10, "prompt_per_second": 100,
                            "predicted_per_second": 20}})
        if m == "no-es-391":
            return self._envia(200, {
                "choices": [{"message": {"content": "No es 391"}, "finish_reason": "stop"}],
                "timings": {"prompt_n": 10, "prompt_per_second": 100,
                            "predicted_per_second": 20}})
        if m == "negativo":
            return self._envia(200, {
                "choices": [{"message": {"content": "-391"}, "finish_reason": "stop"}],
                "timings": {"prompt_n": 10, "prompt_per_second": 100,
                            "predicted_per_second": 20}})
        if m == "cuerpo-no-json":
            return self._envia(200, b"<html>502 Bad Gateway</html>")
        if m == "error-500":
            return self._envia(500, {"error": {"message": "interno"}})
        if m == "por-limite":
            return self._envia(200, {
                "choices": [{"message": {"content": "391 y sigo hablan"},
                             "finish_reason": "length"}],
                "timings": {"prompt_n": 10, "prompt_per_second": 100,
                            "predicted_per_second": 20, "predicted_n": 64}})

        # normal: responde 391 y finaliza bien
        return self._envia(200, {
            "choices": [{"message": {"content": "391"}, "finish_reason": "stop"}],
            "timings": {"prompt_n": 10, "prompt_per_second": 120.5,
                        "predicted_per_second": 25.4, "predicted_n": 3}})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Falso)
        cls.puerto = cls.srv.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.puerto}"
        cls.hilo = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.hilo.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        MODO["v"] = "normal"


class SmokeTest(Base):
    """El smoke test debe salir != 0 en cada respuesta mala. Este era el fallo
    mas grave: daba CORRECTO a '-391', 'No es 391', '391%' y '391 unidades'."""

    def corre(self):
        entorno = dict(os.environ, LLAMA_URL=self.url, LLAMA_API_KEY=CLAVE)
        return subprocess.run(["bash", os.path.join(SCRIPTS, "smoke-test.sh")],
                              capture_output=True, text=True, timeout=120,
                              env=entorno)

    def test_normal_sale_cero(self):
        p = self.corre()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_rechaza_negativo(self):
        MODO["v"] = "negativo"
        self.assertNotEqual(self.corre().returncode, 0)

    def test_rechaza_no_es_391(self):
        MODO["v"] = "no-es-391"
        self.assertNotEqual(self.corre().returncode, 0)

    def test_rechaza_contenido_vacio(self):
        MODO["v"] = "contenido-vacio"
        self.assertNotEqual(self.corre().returncode, 0)

    def test_rechaza_json_vacio(self):
        MODO["v"] = "json-vacio"
        self.assertNotEqual(self.corre().returncode, 0)

    def test_rechaza_finalizacion_por_limite(self):
        # el smoke test SI exige finalizacion normal: es su contrato
        MODO["v"] = "por-limite"
        self.assertNotEqual(self.corre().returncode, 0)

    def test_rechaza_error_http(self):
        MODO["v"] = "error-500"
        self.assertNotEqual(self.corre().returncode, 0)

    def test_rechaza_salud_mala(self):
        MODO["v"] = "salud-mala"
        self.assertNotEqual(self.corre().returncode, 0)

    def test_detecta_servidor_sin_autenticacion(self):
        # un servidor que acepta cualquier clave es un fallo de seguridad
        # que el smoke test tiene que cantar
        MODO["v"] = "sin-auth"
        p = self.corre()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("esperado 401", p.stdout)


class BenchContext(Base):
    """bench-context.py debe salir 2 cuando no consigue medidas, no 0."""

    def corre(self, *extra):
        entorno = dict(os.environ, LLAMA_API_KEY=CLAVE)
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "bench-context.py"),
             "--url", self.url, "--tokens", "200", "--passes", "1", *extra],
            capture_output=True, text=True, timeout=180, env=entorno)

    def test_normal_sale_cero(self):
        p = self.corre()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_json_vacio_no_se_cuenta_como_medida(self):
        MODO["v"] = "json-vacio"
        p = self.corre()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        # y no debe aparecer una fila de ceros como si fuera una medida
        self.assertNotIn("| 0.0 | 0.00 |", p.stdout)

    def test_sin_timings_es_fallo(self):
        MODO["v"] = "sin-timings"
        self.assertEqual(self.corre().returncode, 2)

    def test_cuerpo_no_json_es_fallo(self):
        MODO["v"] = "cuerpo-no-json"
        self.assertEqual(self.corre().returncode, 2)

    def test_escribe_jsonl_crudo(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "crudo.jsonl")
            p = self.corre("--jsonl", ruta)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with open(ruta, encoding="utf-8") as f:
                lineas = [json.loads(x) for x in f if x.strip()]
            # 1 calentamiento + 1 medida, y el calentamiento debe ir marcado
            self.assertGreaterEqual(len(lineas), 2)
            self.assertTrue(any(x.get("warmup") for x in lineas))
            self.assertTrue(any(not x.get("warmup") for x in lineas))


if __name__ == "__main__":
    unittest.main(verbosity=2)
