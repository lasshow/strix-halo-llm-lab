#!/usr/bin/env python3
"""Pruebas de las fases H-032 (cache de prompt) y H-033 (A/B de builds).

Ni GPU, ni modelo, ni M5: el "binario" es `tests/banco_falso.py` copiado dentro
del arbol de builds, que se hace pasar por `llama-server`, deduce su brazo de su
propia ruta y responde lo que le diga un JSON de guion. Los servidores de banco
se lanzan COMO PROCESOS, que es como se lanzan de verdad, y la unidad se lee de
un fichero real con el ExecStart REAL de produccion (copiado de
`evidencias/campana-20260910/resultados.json`).

    python3 tests/test_h032_h033.py -v

Criterio de aceptacion (docs/metodologia.md, regla 8): toda prueba de aqui
FALLA contra el commit 9a0c7f2, el anterior a este lote.
"""
import importlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(RAIZ, "scripts")
AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

from validacion import ErrorInfraestructura, FalloContrato  # noqa: E402


def modulo(nombre):
    """Import PEREZOSO, por el mismo motivo que en test_p0.py: contra el commit
    anterior al lote, un import en la cabecera mata la bateria entera de
    ImportError y todas las pruebas 'fallan' por la misma linea. Cargando cada
    modulo donde se usa, cada prueba falla por lo que tiene que fallar."""
    return importlib.import_module(nombre)


CLAVE = "clave-de-banco-0001"
UNIDAD = "llama-pruebas"
MODELO = "qwen3.8-flash-next"          # el --alias de la unidad real
BASE_SHA, CAND_SHA = "base0000", "cand1111"

# ExecStart REAL de la unidad productiva. La lista que tiene que salir de
# `args_de_unidad` esta en evidencias/campana-20260910/resultados.json
# ("args_prod"), escrita por la campana H-031 desde la maquina.
UNIDAD_REAL = """[Unit]
Description=llama.cpp server - Qwen3.8 Flash Next

[Service]
Type=simple
ExecStart=/models/llama-current/build/bin/llama-server \\
  --model /models/gguf/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf \\
  --mmproj /models/gguf/qwen38-flash-next/mmproj-F16.gguf \\
  --alias qwen3.8-flash-next \\
  -ngl 99 \\
  -c 262144 \\
  -np 2 \\
  -fa on \\
  --cache-reuse 256 \\
  --cache-ram 4096 \\
  -kvu \\
  --batch-size 4096 \\
  --ubatch-size 2048 \\
  --no-context-shift \\
  --lazy-mode off \\
  --threads 16 \\
  --host 0.0.0.0 --port 8080 \\
  --api-key-file /etc/llama-server/api-keys.txt \\
  --metrics
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


def args_prod_de_evidencias():
    with open(os.path.join(RAIZ, "evidencias", "campana-20260910",
                           "resultados.json"), encoding="utf-8") as f:
        return json.load(f)["args_prod"]


def puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ===========================================================================
# El banco de mentira: unidad en disco, builds con el binario falso y corpus
# ===========================================================================
class BancoFalso:
    """Arbol completo: unidad, dos builds con el binario falso, corpus y guion."""

    def __init__(self, unidad_texto=UNIDAD_REAL):
        self.base = tempfile.mkdtemp(prefix="h032-")
        self.dir_unidades = os.path.join(self.base, "systemd")
        self.dir_builds = os.path.join(self.base, "llama-builds")
        self.salida = os.path.join(self.base, "salida")
        self.corpus = os.path.join(self.base, "corpus")
        os.makedirs(self.dir_unidades)
        os.makedirs(self.salida)
        self.ruta_unidad = os.path.join(self.dir_unidades, f"{UNIDAD}.service")
        with open(self.ruta_unidad, "w", encoding="utf-8") as f:
            f.write(unidad_texto)
        for sha in (BASE_SHA, CAND_SHA):
            destino = os.path.join(self.dir_builds, sha, "build", "bin")
            os.makedirs(destino)
            binario = os.path.join(destino, "llama-server")
            shutil.copy(os.path.join(AQUI, "banco_falso.py"), binario)
            os.chmod(binario, os.stat(binario).st_mode | stat.S_IEXEC)
        self.conf = os.path.join(self.base, "guion.json")
        self.registro_servidor = os.path.join(self.base, "servidor.jsonl")
        self.medidas = os.path.join(self.salida, "medidas.jsonl")
        self.guion({})

    def guion(self, d):
        with open(self.conf, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def binario(self, sha):
        return os.path.join(self.dir_builds, sha, "build", "bin", "llama-server")

    def monta_corpus(self, objetivos=(2048, 8192, 16384, 32768)):
        p = modulo("prompts")
        for o in objetivos:
            # El marcador [[PN=n]] es lo que lee el binario falso para declarar
            # un `prompt_n` coherente con el objetivo: asi `verifica_corpus`
            # comprueba de verdad y no se salta por un numero inventado.
            texto = f"[[PN={o}]] " + p.texto_de(max(4, o // 256))
            p.congela(o, texto, o, self.corpus, modelo=MODELO)

    def texto_unidad(self):
        with open(self.ruta_unidad, encoding="utf-8") as f:
            return f.read()

    def lineas(self, ruta):
        if not os.path.exists(ruta):
            return []
        with open(ruta, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def cuerpos(self):
        return [r["cuerpo"] for r in self.lineas(self.registro_servidor)
                if r.get("evento") == "peticion"]

    def arranques(self):
        return [r for r in self.lineas(self.registro_servidor)
                if r.get("evento") == "arranque"]

    def limpia(self):
        shutil.rmtree(self.base, ignore_errors=True)


class ConBanco(unittest.TestCase):
    """Monta el arbol, el Contexto real de campana.py y acorta las esperas."""

    CORPUS = (2048, 8192, 16384, 32768)

    def setUp(self):
        self.banco = modulo("banco")
        self.campana = modulo("campana")
        self.b = BancoFalso()
        self.addCleanup(self.b.limpia)
        self.b.monta_corpus(self.CORPUS)
        self.puerto = puerto_libre()

        previo = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(previo)))
        os.environ.update(BANCO_FALSO_CONF=self.b.conf,
                          BANCO_FALSO_REGISTRO=self.b.registro_servidor,
                          BANCO_PAUSA="0.05", BANCO_LIMITE_ARRANQUE="20",
                          BANCO_GRACIA="3")
        os.environ.pop("H032_CONTAMINACION", None)

        # La lectura de la unidad es la REAL (campana.lee_unidad); solo se
        # redirige el directorio y se quita el sudo, como en test_p0.
        dir_previo, sudo_previo = self.campana.DIR_UNIDADES, self.campana.SUDO
        self.campana.DIR_UNIDADES = self.b.dir_unidades
        self.campana.SUDO = ""
        self.addCleanup(setattr, self.campana, "DIR_UNIDADES", dir_previo)
        self.addCleanup(setattr, self.campana, "SUDO", sudo_previo)

    def ctx(self):
        args = SimpleNamespace(
            unidad=UNIDAD, modelo=MODELO, puerto_prod=8080,
            puerto_banco=self.puerto, salida=self.b.salida,
            dir_corpus=self.b.corpus, tolerancia_corpus=64)
        c = self.campana.Contexto(
            args, {"baseline": os.path.join(self.b.dir_builds, BASE_SHA),
                   "candidato": os.path.join(self.b.dir_builds, CAND_SHA)},
            CLAVE, self.b.medidas)
        c.log = lambda *a, **k: None
        return c


# ===========================================================================
# 1. La linea de arranque sale de la unidad, y los cambios se aplican
# ===========================================================================
class LineaDeArranqueDeLaUnidad(unittest.TestCase):

    def setUp(self):
        self.banco = modulo("banco")

    def test_args_de_unidad_reproduce_el_execstart_real_de_produccion(self):
        """La referencia no me la invento: es `args_prod` de la campana H-031."""
        self.assertEqual(self.banco.args_de_unidad(UNIDAD_REAL),
                         args_prod_de_evidencias())

    def test_args_de_unidad_deja_fuera_el_binario(self):
        args = self.banco.args_de_unidad(UNIDAD_REAL)
        self.assertNotIn("/models/llama-current/build/bin/llama-server", args)
        self.assertEqual(args[0], "--model")

    def test_args_de_unidad_quita_los_prefijos_de_systemd(self):
        texto = "[Service]\nExecStart=-@/bin/llama-server argv0 --port 8080\n"
        # Con '@', el segundo token es argv[0] y los argumentos empiezan luego.
        self.assertEqual(self.banco.args_de_unidad(texto), ["--port", "8080"])
        texto2 = "[Service]\nExecStart=-/bin/llama-server --port 8080\n"
        self.assertEqual(self.banco.args_de_unidad(texto2), ["--port", "8080"])

    def test_args_de_unidad_se_queda_con_el_ultimo_execstart(self):
        """Regla de systemd con drop-ins: manda el ultimo no vacio."""
        texto = ("[Service]\nExecStart=/bin/llama-server --port 8080\n"
                 "ExecStart=/bin/llama-server --port 9931 --metrics\n")
        self.assertEqual(self.banco.args_de_unidad(texto),
                         ["--port", "9931", "--metrics"])

    def test_una_unidad_sin_execstart_es_error_de_infraestructura(self):
        with self.assertRaises(ErrorInfraestructura) as c:
            self.banco.args_de_unidad("[Service]\nRestart=on-failure\n")
        self.assertIn("ExecStart", str(c.exception))

    def test_con_cambios_sustituye_cache_ram_y_puerto_sin_tocar_el_resto(self):
        args = self.banco.args_de_unidad(UNIDAD_REAL)
        nuevos = self.banco.con_cambios(args, cache_ram=12288, port=8099)
        self.assertEqual(self.banco.valor_de(nuevos, "cache_ram"), "12288")
        self.assertEqual(self.banco.valor_de(nuevos, "port"), "8099")
        self.assertNotIn("4096", nuevos[nuevos.index("--cache-ram") + 1])
        # el resto de la linea, intacto
        for flag in ("--ubatch-size", "--lazy-mode", "--mmproj", "--metrics"):
            self.assertIn(flag, nuevos)
        self.assertEqual(len(nuevos), len(args))

    def test_con_cambios_quita_kvu_y_lo_vuelve_a_poner(self):
        args = self.banco.args_de_unidad(UNIDAD_REAL)
        self.assertIn("-kvu", args)
        sin = self.banco.con_cambios(args, kvu=False)
        self.assertNotIn("-kvu", sin)
        self.assertIs(self.banco.valor_de(sin, "kvu"), False)
        con = self.banco.con_cambios(sin, kvu=True)
        self.assertIn("-kvu", con)

    def test_con_cambios_anade_el_flag_que_no_estaba(self):
        """Sin ramas 'si esta' / 'si no esta': el resultado es el mismo."""
        args = ["--model", "x.gguf"]
        self.assertEqual(self.banco.con_cambios(args, cache_ram=0),
                         ["--model", "x.gguf", "--cache-ram", "0"])

    def test_con_cambios_entiende_la_forma_pegada_y_los_alias(self):
        args = ["--port=8080", "--parallel=4", "--kv-unified", "--metrics"]
        nuevos = self.banco.con_cambios(args, port=9000, parallel=1, kvu=False)
        self.assertEqual(nuevos, ["--metrics", "--port", "9000", "-np", "1"])

    def test_con_cambios_quita_un_flag_con_None(self):
        args = self.banco.args_de_unidad(UNIDAD_REAL)
        sin = self.banco.con_cambios(args, api_key_file=None)
        self.assertNotIn("--api-key-file", sin)
        self.assertNotIn("/etc/llama-server/api-keys.txt", sin)

    def test_con_cambios_rechaza_una_clave_que_no_conoce(self):
        """Un typo no puede devolver la linea intacta y medir lo viejo."""
        with self.assertRaises(ValueError) as c:
            self.banco.con_cambios(["--model", "x"], cache_rma=0)
        self.assertIn("cache_rma", str(c.exception))

    def test_pon_cache_ram_sustituye_en_el_texto_de_la_unidad(self):
        nuevo = self.banco.pon_cache_ram(UNIDAD_REAL, 12288)
        self.assertIn("--cache-ram 12288", nuevo)
        self.assertNotIn("--cache-ram 4096", nuevo)

    def test_pon_cache_ram_falla_si_el_flag_no_esta(self):
        with self.assertRaises(RuntimeError) as c:
            self.banco.pon_cache_ram("[Service]\nExecStart=/bin/x --port 8080\n", 0)
        self.assertIn("no se habria aplicado", str(c.exception))


# ===========================================================================
# 2. La espera del banco y la vida del proceso
# ===========================================================================
class EsperaDeProcesoMuerto(unittest.TestCase):
    """Aparte del resto a proposito: `espera_proceso` vive en un modulo que ya
    existia, asi que contra el commit anterior esta prueba tiene que fallar por
    lo suyo (la funcion no esta) y no por el ImportError de un modulo nuevo."""

    def test_un_proceso_ya_muerto_es_error_de_infraestructura(self):
        salud = modulo("salud")
        p = subprocess.Popen(["/bin/true"])
        p.wait()
        with self.assertRaises(ErrorInfraestructura) as c:
            salud.espera_proceso(p, puerto_libre(), modelo=MODELO, clave=CLAVE,
                                 limite=30, pausa=0.05, traza=lambda *a: None)
        self.assertIn("murio", str(c.exception))
        self.assertIn("arranque fallido", str(c.exception))


class FasesEnchufables(unittest.TestCase):
    """Las cuatro fases se cargan por el mismo camino que usara el runner."""

    ESPECIFICACIONES = (
        "fases_h032.py:correccion_cache_ram",
        "fases_h032.py:regresion_28495",
        "fases_h032.py:rendimiento_cache_ram",
        "fases_h033.py:ab_builds",
    )

    def test_campana_carga_cada_fase_por_su_especificacion(self):
        campana = modulo("campana")
        for spec in self.ESPECIFICACIONES:
            with self.subTest(spec=spec):
                self.assertTrue(callable(campana.carga_fase(spec)))

    def test_los_umbrales_estan_escritos_en_los_docstrings(self):
        """El plan de pruebas los copia de aqui: si no estan, no hay umbral
        declarado antes de medir y la campana no significa nada."""
        h32, h33 = modulo("fases_h032"), modulo("fases_h033")
        self.assertIn("contaminaciones == 0", h32.__doc__)
        self.assertIn("20 %", h32.__doc__)
        self.assertIn("1,05x", h32.__doc__)
        self.assertIn("1,05x", h33.__doc__)
        self.assertIn("0,98x", h33.__doc__)
        self.assertEqual((h32.UMBRAL_CAIDA_PP, h32.UMBRAL_TTFT), (0.80, 1.05))
        self.assertEqual((h33.UMBRAL_PP, h33.UMBRAL_TG), (1.05, 0.98))


class EsperaYVidaDelProceso(ConBanco):
    CORPUS = (2048,)

    def _srv(self, binario, args=None, **kw):
        opciones = dict(modelo=MODELO, clave=CLAVE, limite=8, pausa=0.05,
                        gracia=2, log=lambda *a: None, dir_log=self.b.salida)
        opciones.update(kw)
        if args is None:      # la linea productiva, que es la que trae --alias
            args = self.banco.args_de_unidad(UNIDAD_REAL)
        return self.banco.ServidorBanco(binario, list(args), self.puerto, **opciones)

    def dormilon(self):
        ruta = os.path.join(self.b.base, "dormilon")
        with open(ruta, "w") as f:
            f.write("#!/bin/sh\n# ignora sus argumentos y no sirve nada\nexec sleep 600\n")
        os.chmod(ruta, os.stat(ruta).st_mode | stat.S_IEXEC)
        return ruta

    def test_espera_proceso_verde_cuando_sirve_el_modelo(self):
        salud = modulo("salud")
        with self._srv(self.b.binario(BASE_SHA)) as srv:
            self.assertTrue(salud.espera_proceso(
                srv.proc, self.puerto, modelo=MODELO, clave=CLAVE,
                limite=5, pausa=0.05, traza=lambda *a: None))

    def test_espera_proceso_no_da_verde_a_otro_modelo(self):
        salud = modulo("salud")
        with self._srv(self.b.binario(BASE_SHA)) as srv:
            self.assertFalse(salud.espera_proceso(
                srv.proc, self.puerto, modelo="otro-modelo", clave=CLAVE,
                limite=0.6, pausa=0.05, traza=lambda *a: None))

    def test_el_banco_mata_el_proceso_aunque_la_espera_no_de_verde(self):
        """Un servidor de banco que sobrevive a su fase es justo lo que hacia
        que 'produccion restaurada' diera verde en H-031 (fallo B)."""
        srv = self._srv(self.dormilon(), limite=0.6)
        with self.assertRaises(ErrorInfraestructura):
            srv.__enter__()
        self.assertIsNotNone(srv.ultimo_pid)
        with self.assertRaises(ProcessLookupError):
            os.kill(srv.ultimo_pid, 0)

    def test_el_banco_mata_el_proceso_si_el_cuerpo_del_with_revienta(self):
        with self.assertRaises(ZeroDivisionError):
            with self._srv(self.b.binario(BASE_SHA)) as srv:
                pid = srv.ultimo_pid
                1 / 0
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_un_binario_que_muere_al_arrancar_no_agota_el_limite(self):
        self.b.guion({"morir": 1})
        srv = self._srv(self.b.binario(BASE_SHA), limite=30)
        with self.assertRaises(ErrorInfraestructura) as c:
            srv.__enter__()
        self.assertIn("murio", str(c.exception))

    def test_la_clave_va_en_fichero_600_y_se_borra_al_cerrar(self):
        with self._srv(self.b.binario(BASE_SHA)) as srv:
            ruta = srv.ultimo_fichero_clave
            self.assertTrue(os.path.exists(ruta))
            self.assertEqual(stat.S_IMODE(os.stat(ruta).st_mode), 0o600)
            self.assertNotIn("--api-key", " ".join(srv.args_finales).replace(
                "--api-key-file", ""))
        self.assertFalse(os.path.exists(ruta))
        self.assertEqual(self.b.arranques()[0]["origen_clave"], "fichero")

    def test_la_clave_va_por_entorno_si_el_binario_lo_soporta(self):
        self.b.guion({"soporta_env_api_key": True})
        with self._srv(self.b.binario(BASE_SHA)) as srv:
            self.assertEqual(srv.origen_clave, "LLAMA_ARG_API_KEY")
            self.assertIsNone(srv.ultimo_fichero_clave)
            self.assertNotIn("--api-key-file", srv.args_finales)
        self.assertEqual(self.b.arranques()[0]["origen_clave"], "entorno")

    def test_la_clave_nunca_viaja_en_argv(self):
        """H-021: argv es legible por cualquier usuario local."""
        for guion in ({}, {"soporta_env_api_key": True}):
            with self.subTest(guion=guion):
                self.b.guion(guion)
                with self._srv(self.b.binario(BASE_SHA)) as srv:
                    self.assertNotIn(CLAVE, " ".join(srv.args_finales))
                for a in self.b.arranques():
                    self.assertNotIn(CLAVE, " ".join(a["args"]))

    def test_el_banco_escucha_en_su_puerto_y_no_en_el_de_la_unidad(self):
        with self._srv(self.b.binario(BASE_SHA)) as srv:
            self.assertEqual(srv.url, f"http://127.0.0.1:{self.puerto}")
            self.assertTrue(self.banco.puerto_ocupado(self.puerto))
            self.assertGreater(srv.rss_mb(), 0)
        arranque = self.b.arranques()[0]
        self.assertEqual(arranque["port"], self.puerto)
        self.assertEqual(arranque["host"], "127.0.0.1")

    def test_el_puerto_queda_libre_al_salir(self):
        with self._srv(self.b.binario(BASE_SHA)):
            pass
        self.assertTrue(self.banco.espera_puerto_libre(
            self.puerto, limite=5, pausa=0.1, traza=lambda *a: None))

    def test_soporta_env_api_key_pregunta_a_la_ayuda_del_binario(self):
        self.b.guion({"soporta_env_api_key": True})
        self.assertTrue(self.banco.soporta_env_api_key(self.b.binario(BASE_SHA)))
        self.b.guion({})
        self.assertFalse(self.banco.soporta_env_api_key(self.b.binario(BASE_SHA)))
        self.assertFalse(self.banco.soporta_env_api_key("/no/existe/llama-server"))


# ===========================================================================
# 3. El contrato de aislamiento de cache
# ===========================================================================
class ContratoDeNonce(unittest.TestCase):

    def respuesta(self, contenido, fin="stop"):
        return {"choices": [{"message": {"content": contenido},
                             "finish_reason": fin}]}

    def nonce(self, *a, **k):
        return modulo("validacion").recuperacion_nonce(*a, **k)

    def test_el_nonce_propio_cumple_el_contrato(self):
        r = self.nonce(self.respuesta("a1b2c3d4e5f6"), "a1b2c3d4e5f6",
                       ["111111111111"])
        self.assertEqual(r["nonce"], "a1b2c3d4e5f6")
        self.assertEqual(r["intrusos"], [])

    def test_el_nonce_propio_en_prosa_tambien_cumple(self):
        """Aqui se mide la cache, no el formato: exigir igualdad exacta
        apuntaria como fuga de contexto una respuesta correcta."""
        self.assertIsNotNone(
            self.nonce(self.respuesta("El código es A1B2C3D4E5F6."),
                       "a1b2c3d4e5f6", ["111111111111"]))

    def test_un_nonce_ajeno_es_contaminacion_y_lo_dice(self):
        with self.assertRaises(FalloContrato) as c:
            self.nonce(self.respuesta("111111111111"), "a1b2c3d4e5f6",
                       ["111111111111", "222222222222"])
        self.assertEqual(c.exception.motivo, "contaminacion")
        self.assertIn("nonce ajeno", str(c.exception))
        self.assertIn("111111111111", str(c.exception))

    def test_el_ajeno_manda_aunque_venga_con_el_propio(self):
        with self.assertRaises(FalloContrato) as c:
            self.nonce(self.respuesta("a1b2c3d4e5f6 y 111111111111"),
                       "a1b2c3d4e5f6", ["111111111111"])
        self.assertEqual(c.exception.motivo, "contaminacion")

    def test_sin_ningun_nonce_es_no_recupera_y_no_contaminacion(self):
        with self.assertRaises(FalloContrato) as c:
            self.nonce(self.respuesta("no lo recuerdo"), "a1b2c3d4e5f6",
                       ["111111111111"])
        self.assertEqual(c.exception.motivo, "no_recupera")
        self.assertEqual(c.exception.intrusos, [])


# ===========================================================================
# 4. H-032 fase 1: correccion de la cache
# ===========================================================================
class CorreccionDeCache(ConBanco):
    CORPUS = (2048,)

    def setUp(self):
        super().setUp()
        os.environ.update(H032_CACHE_RAM="0,4096", H032_CICLOS="4")
        self.fases = modulo("fases_h032")

    def corre(self, guion):
        base = {"prompt_ms_frio": 900.0, "prompt_ms_cache": 120.0}
        base.update(guion)
        self.b.guion(base)
        return self.fases.correccion_cache_ram(self.ctx())

    def test_modo_sano_no_reporta_contaminaciones_y_adopta(self):
        r = self.corre({})
        self.assertNotIn("error", r)
        self.assertTrue(r["adoptar"])
        self.assertEqual(r["resumen"]["contaminaciones_total"], 0)
        for cr in ("0", "4096"):
            self.assertEqual(r["resumen"]["configs"][cr]["contaminaciones"], 0)
            self.assertEqual(r["resumen"]["configs"][cr]["peticiones"], 8)

    def test_modo_contaminado_lo_reporta_y_no_adopta(self):
        r = self.corre({"contamina": True})
        self.assertFalse(r["adoptar"])
        self.assertGreater(r["resumen"]["contaminaciones_total"], 0)
        self.assertIn("nonce ajeno", r["error"])

    def test_una_sola_contaminacion_basta_para_no_adoptar(self):
        r = self.corre({"contamina": True})
        total = r["resumen"]["contaminaciones_total"]
        self.assertGreaterEqual(total, 1)
        self.assertFalse(r["adoptar"], f"{total} contaminaciones y adopto")

    def test_una_config_que_no_arranca_tampoco_acredita_cero(self):
        r = self.corre({"morir": 1})
        self.assertFalse(r["adoptar"])
        self.assertEqual(r["resumen"]["configs_sin_medir"], ["0", "4096"])

    def test_esta_fase_no_devuelve_aplicar(self):
        """Decide si la cache es SEGURA, no cuanta: no toca la unidad."""
        self.assertNotIn("aplicar", self.corre({}))

    def test_registra_una_medida_por_peticion_con_prompt_n_y_timings(self):
        self.corre({})
        medidas = self.b.lineas(self.b.medidas)
        ciclos = [m for m in medidas if m.get("etapa") == "ciclo"]
        self.assertEqual(len(ciclos), 16)          # 2 configs x 4 ciclos x 2
        for m in ciclos:
            self.assertEqual(m["fase"], "h032-correccion")
            self.assertEqual(m["brazo"], "baseline")
            self.assertIn("cache-ram", m["config"])
            self.assertEqual(m["prompt_n"], 2048)
            self.assertIn("prompt_per_second", m["timings"])
            self.assertIn("cache_n", m["timings"])
            self.assertIsNotNone(m["prompt_ms"])
            self.assertEqual(len(m["esperado"]), 12)
            self.assertIn(m["esperado"].upper(), m["obtenido"].upper())

    def test_las_peticiones_van_con_cache_y_sin_pensamiento(self):
        self.corre({})
        cuerpos = self.b.cuerpos()
        self.assertTrue(cuerpos)
        for c in cuerpos:
            self.assertIs(c["cache_prompt"], True)
            self.assertEqual(c["temperature"], 0)
            self.assertEqual(c["chat_template_kwargs"], {"enable_thinking": False})
        preguntas = [c for c in cuerpos
                     if "¿Cuál era el CÓDIGO?" in c["messages"][-1]["content"]]
        self.assertEqual(len(preguntas), 16)
        self.assertTrue(all(c["max_tokens"] == 32 for c in preguntas))

    def test_el_resumen_trae_ttft_con_y_sin_cache_rss_y_cache_n(self):
        r = self.corre({})
        c = r["resumen"]["configs"]["4096"]
        self.assertEqual(c["ttft_sin_cache_ms"], 900.0)
        self.assertEqual(c["ttft_con_cache_ms"], 120.0)
        self.assertGreater(c["rss_mb"], 0)
        self.assertIsNotNone(c["cache_n_medio"])
        self.assertEqual(c["ciclos"], 4)

    def test_los_ciclos_preguntan_a_dos_conversaciones_distintas(self):
        self.corre({})
        ciclos = [m for m in self.b.lineas(self.b.medidas)
                  if m.get("etapa") == "ciclo"]
        for m in ciclos:
            self.assertEqual(len(m["concurrente_con"]), 1)
            self.assertNotEqual(m["concurrente_con"][0], m["conversacion"])


# ===========================================================================
# 5. H-032 fase 2: la regresion #28495, como hipotesis
# ===========================================================================
class Regresion28495(ConBanco):
    CORPUS = (16384,)

    def setUp(self):
        super().setUp()
        self.fases = modulo("fases_h032")

    def corre(self, factor):
        self.b.guion({"pp": 300.0, "tg": 25.0, "pp_factor_desde_2": factor})
        return self.fases.regresion_28495(self.ctx())

    def test_pp_que_cae_a_la_mitad_desde_la_segunda_marca_regresion(self):
        r = self.corre(0.5)
        self.assertTrue(r["resumen"]["regresion_detectada"])
        self.assertTrue(any("cae al" in m for m in r["resumen"]["motivos"]))
        for nombre in ("np1", "np2-kvu", "np2"):
            self.assertAlmostEqual(
                r["resumen"]["configs"][nombre]["ratio_2mas_vs_1"], 0.5, places=3)

    def test_pp_estable_no_marca_regresion(self):
        r = self.corre(1.0)
        self.assertFalse(r["resumen"]["regresion_detectada"])
        self.assertEqual(r["resumen"]["motivos"], [])

    def test_esta_fase_no_vota(self):
        """Es diagnostico: una hipotesis sobre HIP/ROCm en una pila Vulkan no
        puede tumbar por si sola la promocion de nada."""
        r = self.corre(1.0)
        self.assertNotIn("adoptar", r)
        self.assertNotIn("aplicar", r)

    def test_descarta_la_primera_y_mide_las_otras_tres_en_cada_config(self):
        self.corre(1.0)
        medidas = [m for m in self.b.lineas(self.b.medidas)
                   if m.get("fase") == "h032-28495"]
        self.assertEqual(len(medidas), 12)         # 3 configs x 4 peticiones
        self.assertEqual(sum(1 for m in medidas if m["descartada"]), 3)
        for m in medidas:
            self.assertEqual(m["prompt_n"], 16384)
            self.assertIs(m["timings"]["cache_n"], 0)

    def test_cada_config_arranca_con_su_np_y_su_kvu(self):
        self.corre(1.0)
        arranques = {(a["parallel"], a["kvu"]) for a in self.b.arranques()}
        self.assertEqual(arranques, {(1, False), (2, True), (2, False)})

    def test_las_peticiones_largas_van_sin_reutilizar_cache(self):
        """Metodologia, regla 9: con reutilizacion de prefijo el pp no mide."""
        self.corre(1.0)
        for c in self.b.cuerpos():
            self.assertIs(c["cache_prompt"], False)
            self.assertEqual(c["max_tokens"], 64)

    def test_una_config_que_no_arranca_es_error_y_no_una_regresion(self):
        self.b.guion({"morir": 1})
        r = self.fases.regresion_28495(self.ctx())
        self.assertFalse(r["resumen"]["regresion_detectada"])
        self.assertIn("no pude medir", r["error"])


# ===========================================================================
# 6. H-032 fase 3: cuanto compra la cache grande
# ===========================================================================
class RendimientoDeCacheRam(ConBanco):
    CORPUS = (8192,)

    def setUp(self):
        super().setUp()
        self.fases = modulo("fases_h032")

    def corre(self, ttft_4096, ttft_12288):
        self.b.guion({"prompt_ms_frio": 900.0,
                      "prompt_ms_cache": {"4096": ttft_4096,
                                          "12288": ttft_12288}})
        return self.fases.rendimiento_cache_ram(self.ctx())

    def test_adopta_si_12288_no_es_peor_y_aplicar_lo_escribe(self):
        r = self.corre(100.0, 90.0)
        self.assertTrue(r["adoptar"])
        self.assertAlmostEqual(r["resumen"]["ratio_12288_sobre_4096"], 0.9, places=3)
        self.assertIn("--cache-ram 12288", r["aplicar"](UNIDAD_REAL))

    def test_en_el_limite_justo_del_umbral_todavia_adopta(self):
        r = self.corre(100.0, 105.0)
        self.assertTrue(r["adoptar"])
        self.assertIn("--cache-ram 12288", r["aplicar"](UNIDAD_REAL))

    def test_no_adopta_si_12288_es_peor_y_aplicar_baja_a_4096(self):
        r = self.corre(100.0, 200.0)
        self.assertFalse(r["adoptar"])
        self.assertIn("--cache-ram 4096", r["aplicar"](UNIDAD_REAL))
        self.assertNotIn("12288", r["aplicar"](UNIDAD_REAL))

    def test_con_contaminacion_declarada_aplicar_apaga_la_cache(self):
        """La limitacion documentada: sin estado entre fases, la fuga se
        declara por entorno y aqui se obedece."""
        os.environ["H032_CONTAMINACION"] = "1"
        r = self.corre(100.0, 90.0)
        self.assertEqual(r["resumen"]["cache_ram_a_aplicar"], 0)
        self.assertTrue(r["resumen"]["contaminacion_declarada"])
        self.assertIn("--cache-ram 0", r["aplicar"](UNIDAD_REAL))

    def test_mide_cinco_repeticiones_mas_el_cebado(self):
        self.corre(100.0, 90.0)
        medidas = [m for m in self.b.lineas(self.b.medidas)
                   if m.get("fase") == "h032-rendimiento"]
        self.assertEqual(len(medidas), 12)          # 2 configs x (1 cebado + 5)
        self.assertEqual(sum(1 for m in medidas if m["cebado"]), 2)
        for m in medidas:
            self.assertEqual(m["prompt_n"], 8192)
        calientes = [m for m in medidas if not m["cebado"]]
        self.assertTrue(all(m["cache_n"] == 8192 for m in calientes))

    def test_no_adopta_si_falta_una_de_las_dos_configuraciones(self):
        self.b.guion({"morir": 1})
        r = self.fases.rendimiento_cache_ram(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIsNone(r["resumen"]["ratio_12288_sobre_4096"])
        self.assertIn("cache-ram", r["error"])


# ===========================================================================
# 7. H-033: A/B de builds con igualdad greedy
# ===========================================================================
class ABDeBuilds(ConBanco):
    CORPUS = (8192, 32768)

    def setUp(self):
        super().setUp()
        self.fases = modulo("fases_h033")

    def corre(self, pp_cand=1.20, tg_cand=1.0, greedy_distinto=False):
        guion = {
            "pp": {"por_defecto": 300.0, CAND_SHA: 300.0 * pp_cand},
            "tg": {"por_defecto": 25.0, CAND_SHA: 25.0 * tg_cand},
            "greedy_prefijo": {"por_defecto": "G",
                               CAND_SHA: "H" if greedy_distinto else "G"},
        }
        self.b.guion(guion)
        return self.fases.ab_builds(self.ctx())

    def test_candidato_un_20_por_ciento_mejor_adopta(self):
        r = self.corre(pp_cand=1.20)
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertNotIn("error", r)
        for t in ("8192", "32768"):
            self.assertAlmostEqual(r["resumen"]["ratios"][t]["pp"], 1.2, places=3)
        self.assertTrue(r["resumen"]["greedy"]["identico"])

    def test_candidato_un_2_por_ciento_mejor_no_llega_al_umbral(self):
        r = self.corre(pp_cand=1.02)
        self.assertFalse(r["adoptar"])
        self.assertIn("por debajo del umbral", r["error"])
        self.assertIn("1.020x", r["error"])

    def test_un_peaje_de_generacion_tumba_la_adopcion(self):
        r = self.corre(pp_cand=1.20, tg_cand=0.90)
        self.assertFalse(r["adoptar"])
        self.assertIn("tg a 8192", r["error"])

    def test_salida_greedy_distinta_no_adopta_y_lo_explica(self):
        """La PR promete salida greedy identica: si el texto cambia no es una
        build mas rapida, es otra build."""
        r = self.corre(pp_cand=1.20, greedy_distinto=True)
        self.assertFalse(r["adoptar"])
        self.assertIn("greedy", r["error"])
        self.assertFalse(r["resumen"]["greedy"]["identico"])
        self.assertEqual(r["resumen"]["greedy"]["indices_distintos"], [0, 1, 2])

    def test_alterna_los_brazos_A_B_A_B(self):
        r = self.corre()
        self.assertEqual(r["resumen"]["orden"],
                         ["baseline", "candidato", "baseline", "candidato"])
        self.assertEqual(
            [a["brazo"] for a in self.b.arranques()],
            [BASE_SHA, CAND_SHA, BASE_SHA, CAND_SHA])

    def test_los_dos_brazos_arrancan_con_la_misma_linea_productiva(self):
        """Cualquier otra diferencia entre brazos invalida el punto."""
        self.corre()
        # El fichero temporal de la credencial es distinto en cada arranque a
        # proposito (se borra al cerrar); lo demas tiene que ser identico.
        lineas = {tuple(self.banco.con_cambios(a["args"], api_key_file=None))
                  for a in self.b.arranques()}
        self.assertEqual(len(lineas), 1, "los brazos no midieron la misma config")
        args = list(lineas.pop())
        self.assertIn("--ubatch-size", args)
        self.assertEqual(args[args.index("--cache-ram") + 1], "4096")

    def test_mide_los_dos_tamanos_en_las_dos_rondas(self):
        self.corre()
        medidas = [m for m in self.b.lineas(self.b.medidas)
                   if m.get("fase") == "h033-ab-builds"]
        self.assertEqual(len(medidas), 8)           # 2 brazos x 2 rondas x 2
        for m in medidas:
            self.assertEqual(m["prompt_n"], m["tamano"])
            self.assertEqual(m["timings"]["cache_n"], 0)
        for brazo in ("baseline", "candidato"):
            for t in (8192, 32768):
                self.assertEqual(
                    sum(1 for m in medidas
                        if m["brazo"] == brazo and m["tamano"] == t), 2)

    def test_los_prompts_greedy_van_con_seed_y_temperatura_cero(self):
        self.corre()
        greedy = [c for c in self.b.cuerpos() if c.get("seed") is not None]
        self.assertEqual(len(greedy), 6)            # 3 prompts x 2 brazos
        for c in greedy:
            self.assertEqual(c["seed"], 42)
            self.assertEqual(c["temperature"], 0)
            self.assertEqual(c["max_tokens"], 64)

    def test_esta_fase_no_devuelve_aplicar(self):
        """La promocion de build la hace el runner, no la fase."""
        self.assertNotIn("aplicar", self.corre())

    def test_si_un_brazo_no_arranca_no_adopta_y_lo_dice(self):
        self.b.guion({"morir": 1})
        r = self.fases.ab_builds(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIsNone(r["resumen"]["greedy"]["identico"])
        self.assertIn("arranque fallido", r["error"])
        self.assertEqual(len(r["resumen"]["errores"]), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
