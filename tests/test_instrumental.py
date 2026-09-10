#!/usr/bin/env python3
"""Pruebas del INSTRUMENTAL. Fallan si el banco vuelve a dar por buenas las
respuestas que motivaron la auditoria externa (H-024).

Por que existen: la correccion de un instrumento de medida no se puede
"declarar". Cada caso que antes pasaba en verde esta aqui como una prueba que
tiene que FALLAR como toca. Se cubre resultado registrado Y codigo de salida.

    python3 tests/test_instrumental.py -v

No necesita GPU, ni el modelo, ni el M5: levanta un servidor HTTP de mentira
y sustituye systemd por funciones controladas.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(RAIZ, "scripts")
sys.path.insert(0, SCRIPTS)

from validacion import (ErrorInfraestructura, FalloContrato, cuerpo_json,  # noqa: E402
                        generacion_medida, llamada_herramienta,
                        respuesta_exacta, respuesta_final, timings)


def carga(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = carga("bench_ubatch", os.path.join(SCRIPTS, "bench-ubatch.py"))

TIMINGS_OK = {"prompt_n": 5000, "prompt_per_second": 400.0,
              "predicted_per_second": 25.0, "predicted_n": 160}


def respuesta(content=None, fin="stop", tm=TIMINGS_OK, razon=None, tools=None):
    msg = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if razon is not None:
        msg["reasoning_content"] = razon
    if tools is not None:
        msg["tool_calls"] = tools
    d = {"choices": [{"message": msg, "finish_reason": fin}]}
    if tm is not None:
        d["timings"] = tm
    return d


# --------------------------------------------------------------------------
class RespuestasQueAntesPasaban(unittest.TestCase):
    """Los casos exactos que la auditoria demostro que se colaban."""

    def test_cuerpo_vacio_no_es_medida_de_cero(self):
        # Antes: d.get("timings", {}) -> (0, 0, 0) y la fila se marcaba 'ok'.
        with self.assertRaises(ErrorInfraestructura):
            generacion_medida({}, 160)

    def test_timings_ausentes_es_error_no_cero(self):
        with self.assertRaises(ErrorInfraestructura):
            timings({"choices": [{"message": {"content": "hola"}}]})

    def test_timings_incompletos(self):
        with self.assertRaises(ErrorInfraestructura):
            timings({"timings": {"prompt_n": 10}})

    def test_timings_no_plausibles(self):
        with self.assertRaises(ErrorInfraestructura):
            timings({"timings": {"prompt_n": 0, "prompt_per_second": 0.0,
                                 "predicted_per_second": 0.0}})

    def test_content_vacio_con_razonamiento_es_fallo(self):
        # H-019: bucle de razonamiento. El cliente recibe una respuesta vacia.
        with self.assertRaises(FalloContrato) as c:
            respuesta_final(respuesta(content="", razon="x" * 4000))
        self.assertIn("razonamiento", str(c.exception))

    def test_content_ausente_del_todo(self):
        with self.assertRaises(FalloContrato):
            respuesta_final(respuesta(content=None))

    def test_json_roto_es_infraestructura(self):
        with self.assertRaises(ErrorInfraestructura):
            cuerpo_json(b"<html>502 Bad Gateway</html>")

    def test_error_del_servidor_es_infraestructura(self):
        with self.assertRaises(ErrorInfraestructura):
            cuerpo_json(json.dumps({"error": {"message": "context too large"}}))

    def test_sin_choices_es_infraestructura(self):
        with self.assertRaises(ErrorInfraestructura):
            respuesta_final({"timings": TIMINGS_OK})


class ContratoExacto391(unittest.TestCase):
    """El findall(r'\\d+') anterior aceptaba todo esto menos '1391'."""

    def test_acepta_solo_el_valor_exacto(self):
        self.assertEqual(respuesta_exacta(respuesta("391"), "391"), "391")
        self.assertEqual(respuesta_exacta(respuesta(" 391 \n"), "391"), "391")
        self.assertEqual(respuesta_exacta(respuesta("391."), "391"), "391")

    def test_rechaza_los_falsos_positivos_de_la_auditoria(self):
        for malo in ("-391", "No es 391", "391%", "391 unidades",
                     "1391", "39", "El resultado es 391", "391\n391"):
            with self.subTest(respuesta=malo):
                with self.assertRaises(FalloContrato):
                    respuesta_exacta(respuesta(malo), "391")

    def test_rechaza_truncada_aunque_el_texto_cuadre(self):
        with self.assertRaises(FalloContrato):
            respuesta_exacta(respuesta("391", fin="length"), "391")


class ContratosPorTipoDePrueba(unittest.TestCase):
    """No se puede exigir finish_reason='stop' universalmente."""

    def test_benchmark_admite_length_y_declara_los_tokens(self):
        m = generacion_medida(respuesta("texto cortado", fin="length"), 160)
        self.assertTrue(m["truncada"])
        self.assertEqual(m["predicted_n"], 160)
        self.assertEqual(m["tg"], 25.0)

    def test_benchmark_exige_tokens_generados(self):
        tm = dict(TIMINGS_OK, predicted_n=0)
        with self.assertRaises(ErrorInfraestructura):
            generacion_medida(respuesta("x", tm=tm), 160)

    def test_tarea_no_admite_length(self):
        with self.assertRaises(FalloContrato):
            respuesta_final(respuesta("respuesta a medi", fin="length"))

    def test_herramienta_valida_sin_texto(self):
        tc = [{"function": {"name": "leer_sensor",
                            "arguments": '{"id": 7}'}}]
        r = llamada_herramienta(respuesta(content="", fin="tool_calls", tools=tc),
                                "leer_sensor")
        self.assertEqual(r["argumentos"], {"id": 7})

    def test_herramienta_equivocada_o_argumentos_rotos(self):
        tc = [{"function": {"name": "otra", "arguments": "{"}}]
        with self.assertRaises(FalloContrato):
            llamada_herramienta(respuesta(content="", fin="tool_calls", tools=tc), "leer_sensor")


class SaludConsultaLaUnidadCorrecta(unittest.TestCase):
    """El fallo de prioridad alta: espera_salud miraba siempre la de bench."""

    def setUp(self):
        self.consultadas = []
        self.original = bench.sh
        bench.sh = self._sh_falso
        self.estado = "inactive"

    def tearDown(self):
        bench.sh = self.original

    def _sh_falso(self, cmd, check=True):
        if "is-active" in cmd:
            self.consultadas.append(cmd.split()[-1])
            return self.estado
        if "ActiveState" in cmd:
            return self.estado
        return ""

    def test_consulta_la_unidad_que_le_pasan(self):
        bench.espera_salud("llama-flashnext", 8080, limite=1)
        self.assertEqual(set(self.consultadas), {"llama-flashnext"})
        self.assertNotIn("llama-flashnext-bench", self.consultadas)

    def test_corta_pronto_si_la_unidad_esta_muerta(self):
        self.estado = "failed"
        import time
        t0 = time.time()
        self.assertFalse(bench.espera_salud("llama-flashnext", 8080, limite=600))
        self.assertLess(time.time() - t0, 20, "no debe agotar el limite completo")


class ServidorFalso(BaseHTTPRequestHandler):
    guion = {}

    def _responder(self, code, cuerpo):
        b = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self._responder(200, {"status": "ok"})
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {ServidorFalso.guion.get('clave')}":
            return self._responder(401, {"error": "unauthorized"})
        self._responder(200, {"data": [{"id": "modelo-de-prueba"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        code, cuerpo = ServidorFalso.guion.get("chat", (200, respuesta("391")))
        self._responder(code, cuerpo)

    def log_message(self, *a):
        pass


class ServidorEnHilo:
    def __enter__(self):
        self.s = HTTPServer(("127.0.0.1", 0), ServidorFalso)
        self.puerto = self.s.server_address[1]
        threading.Thread(target=self.s.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.s.shutdown()


class PeticionContraServidorReal(unittest.TestCase):
    """una_peticion() contra HTTP de verdad (simulado), no contra mocks."""

    def test_respuesta_buena(self):
        ServidorFalso.guion = {"chat": (200, respuesta("hola", fin="length"))}
        with ServidorEnHilo() as s:
            m = bench.una_peticion(s.puerto, "prompt", 160, timeout=10)
        self.assertEqual(m["pp"], 400.0)
        self.assertTrue(m["truncada"])

    def test_cuerpo_vacio(self):
        ServidorFalso.guion = {"chat": (200, {})}
        with ServidorEnHilo() as s:
            with self.assertRaises(ErrorInfraestructura):
                bench.una_peticion(s.puerto, "prompt", 160, timeout=10)

    def test_http_500(self):
        ServidorFalso.guion = {"chat": (500, {"error": "boom"})}
        with ServidorEnHilo() as s:
            with self.assertRaises(ErrorInfraestructura):
                bench.una_peticion(s.puerto, "prompt", 160, timeout=10)

    def test_html_en_vez_de_json(self):
        ServidorFalso.guion = {"chat": (200, b"<html>proxy</html>")}
        with ServidorEnHilo() as s:
            with self.assertRaises(ErrorInfraestructura):
                bench.una_peticion(s.puerto, "prompt", 160, timeout=10)

    def test_conexion_rechazada(self):
        with self.assertRaises(ErrorInfraestructura):
            bench.una_peticion(1, "prompt", 160, timeout=2)


class SmokeTestDevuelveCodigoCorrecto(unittest.TestCase):
    """El smoke test completo contra el servidor falso, mirando el exit code."""

    def _correr(self, chat):
        ServidorFalso.guion = {"clave": "clave-buena", "chat": chat}
        with ServidorEnHilo() as s:
            env = dict(os.environ, LLAMA_API_KEY="clave-buena")
            return subprocess.run(
                ["bash", os.path.join(SCRIPTS, "smoke-test.sh"),
                 f"http://127.0.0.1:{s.puerto}"],
                capture_output=True, text=True, env=env, timeout=120)

    def test_respuesta_correcta_sale_cero(self):
        r = self._correr((200, respuesta("391")))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_falsos_positivos_salen_uno(self):
        for malo in ("-391", "No es 391", "391%", "391 unidades", "1391"):
            with self.subTest(respuesta=malo):
                r = self._correr((200, respuesta(malo)))
                self.assertEqual(r.returncode, 1, f"{malo!r} paso el smoke test")
                self.assertIn("no es exactamente 391", r.stdout)

    def test_content_vacio_con_razonamiento(self):
        r = self._correr((200, respuesta("", razon="x" * 3000)))
        self.assertEqual(r.returncode, 1)
        self.assertIn("H-019", r.stdout)

    def test_truncada_por_presupuesto(self):
        r = self._correr((200, respuesta("39", fin="length")))
        self.assertEqual(r.returncode, 1)
        self.assertIn("cortada", r.stdout)

    def test_sin_timings_es_fallo(self):
        r = self._correr((200, respuesta("391", tm=None)))
        self.assertEqual(r.returncode, 1, "una respuesta sin metricas no puede dar verde")

    def test_la_clave_no_se_imprime_entera(self):
        r = self._correr((200, respuesta("391")))
        self.assertNotIn("clave-buena", r.stdout + r.stderr)


class CodigosDeSalidaDelBarrido(unittest.TestCase):
    """0 correcto, 2 medida fallida, 3 fallo de restauracion."""

    UNIDAD = ("[Unit]\nDescription=llama.cpp server\n[Service]\n"
              "ExecStart=/bin/llama-server --host 0.0.0.0 --port 8080 "
              "--batch-size 4096 --ubatch-size 1024 --api-key-file /etc/x\n"
              "Restart=on-failure\n")

    def setUp(self):
        self.orig = (bench.sh, bench.unidad_productiva, bench.espera_salud,
                     bench.mide, bench.CLAVE)
        bench.CLAVE = "k"
        bench.unidad_productiva = lambda: self.UNIDAD
        bench.sh = lambda cmd, check=True: (
            "0" if "sudo -n true" in cmd else
            "active" if "is-active" in cmd else "1000")

    def tearDown(self):
        (bench.sh, bench.unidad_productiva, bench.espera_salud,
         bench.mide, bench.CLAVE) = self.orig

    def _argv(self, tmp):
        return ["bench-ubatch.py", "--ubatch", "1024", "--passes", "1",
                "--jsonl", tmp]

    def test_todo_bien_sale_cero(self):
        import tempfile
        bench.espera_salud = lambda u, p, limite=600: True
        bench.mide = lambda *a, **k: [{"prompt_n": 100, "pp": 1.0, "tg": 2.0}]
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            sys.argv = self._argv(f.name)
            self.assertEqual(bench.main(), 0)

    def test_medida_fallida_sale_dos(self):
        import tempfile

        def revienta(*a, **k):
            raise RuntimeError("ErrorInfraestructura: timings ausentes")

        bench.espera_salud = lambda u, p, limite=600: True
        bench.mide = revienta
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            sys.argv = self._argv(f.name)
            self.assertEqual(bench.main(), 2)

    def test_fallo_de_restauracion_sale_tres_y_manda_sobre_el_resto(self):
        import tempfile
        # La medida va bien pero la productiva no vuelve: es lo mas grave.
        bench.espera_salud = lambda u, p, limite=600: u != bench.UNIT_PROD
        bench.mide = lambda *a, **k: [{"prompt_n": 100, "pp": 1.0, "tg": 2.0}]
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            sys.argv = self._argv(f.name)
            self.assertEqual(bench.main(), 3)

    def test_falta_la_clave_sale_uno(self):
        import tempfile
        bench.CLAVE = ""
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            sys.argv = self._argv(f.name)
            self.assertEqual(bench.main(), 1)


class RegistroCrudo(unittest.TestCase):
    """Calentamiento marcado y fallos CONSERVADOS en el JSONL."""

    def test_warmup_no_entra_en_la_mediana_pero_si_en_el_registro(self):
        import io
        llamadas = []

        def falsa(puerto, prompt, max_tokens, timeout=1200):
            llamadas.append(1)
            return {"prompt_n": 100, "pp": float(len(llamadas)), "tg": 1.0,
                    "predicted_n": 10, "finish_reason": "stop",
                    "truncada": False, "presupuesto": max_tokens,
                    "texto": "x", "wall": 0.1}

        orig, bench.una_peticion = bench.una_peticion, falsa
        try:
            buf = io.StringIO()
            ms = bench.mide(8081, 10, 2, 1024, 4096, buf)
        finally:
            bench.una_peticion = orig
        self.assertEqual(len(ms), 2, "el calentamiento no puede contar como medida")
        lineas = [json.loads(x) for x in buf.getvalue().splitlines()]
        self.assertEqual(len(lineas), 3, "el calentamiento SI se registra")
        self.assertTrue(lineas[0]["warmup"])
        self.assertEqual([l["pp"] for l in lineas], [1.0, 2.0, 3.0])

    def test_una_pasada_fallida_no_se_silencia(self):
        import io

        def falsa(*a, **k):
            raise ErrorInfraestructura("timeout tras 1200 s")

        orig, bench.una_peticion = bench.una_peticion, falsa
        try:
            buf = io.StringIO()
            with self.assertRaises(RuntimeError):
                bench.mide(8081, 10, 2, 1024, 4096, buf)
        finally:
            bench.una_peticion = orig
        lineas = [json.loads(x) for x in buf.getvalue().splitlines()]
        self.assertTrue(all(l["fallo"] for l in lineas))
        self.assertIn("timeout", lineas[-1]["fallo"])


class UnidadDePruebasDerivada(unittest.TestCase):
    def test_fija_batch_mueve_ubatch_y_desactiva_el_reinicio(self):
        u = bench.construye_unidad(CodigosDeSalidaDelBarrido.UNIDAD, 512, 4096)
        self.assertIn("--ubatch-size 512", u)
        self.assertIn("--batch-size 4096", u)
        self.assertIn("--port 8081", u)
        self.assertIn("Restart=no", u)
        self.assertNotIn("Restart=on-failure", u)

    def test_falla_si_la_unidad_no_trae_los_parametros(self):
        with self.assertRaises(RuntimeError):
            bench.construye_unidad("[Service]\nExecStart=/bin/llama-server\n", 512, 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
