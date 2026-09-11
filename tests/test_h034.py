#!/usr/bin/env python3
"""Pruebas de la fase H-034: cabeza MTP sidecar, un solo slot, escalera, vision.

Misma tecnica que `test_h032_h033.py`: el "binario" es `tests/banco_falso.py`
copiado en el arbol de builds, que deduce su brazo de su propia ruta y simula
MTP (telemetria de borrador en `timings`, `draft acceptance` en el log), la
caida de GPU del prefill largo (#27306) y la vision multimodal. La linea de
arranque productiva y el Contexto son los reales.

    python3 tests/test_h034.py -v

Criterio de aceptacion (docs/metodologia.md, regla 8): toda prueba de aqui
FALLA contra el commit 1b7a935 (los tres modulos -- `fases_h034.py` nuevo y
los modos MTP del banco falso -- no existen ahi: ImportError/AttributeError
cuenta como fallo, no como ausencia de prueba).
"""
import importlib
import json
import os
import sys
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(RAIZ, "scripts")
AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, AQUI)

from test_h032_h033 import (BASE_SHA, CAND_SHA, CLAVE, MODELO, UNIDAD,  # noqa: E402
                            UNIDAD_REAL, BancoFalso, ConBanco)


def modulo(nombre):
    """Import PEREZOSO: contra 1b7a935 el ImportError tiene que matar la
    prueba por la linea que corresponde, no por la cabecera entera."""
    return importlib.import_module(nombre)


# ===========================================================================
# 1. La linea de arranque MTP: flags sidecar, -np 1, sin -kvu
# ===========================================================================
class LineaDeArranqueMTP(unittest.TestCase):

    def setUp(self):
        self.banco = modulo("banco")
        self.fases = modulo("fases_h034")

    def test_args_mtp_construye_la_linea_sidecar_y_normaliza_el_resto(self):
        base = self.banco.args_de_unidad(UNIDAD_REAL)
        cabeza = "/models/gguf/qwen38-flash-next/MTP/mtp-shared-Q8_0.gguf"
        linea = self.fases.args_mtp(base, 2, cabeza)
        self.assertEqual(self.banco.valor_de(linea, "spec_type"), "draft-mtp")
        self.assertEqual(self.banco.valor_de(linea, "draft_model"), cabeza)
        self.assertEqual(self.banco.valor_de(linea, "spec_draft_n_max"), "2")
        self.assertEqual(self.banco.valor_de(linea, "spec_draft_p_min"), "0.0")
        # la linea comun: un slot, sin -kvu, cache reducida
        self.assertEqual(self.banco.valor_de(linea, "parallel"), "1")
        self.assertIs(self.banco.valor_de(linea, "kvu"), False)
        self.assertEqual(self.banco.valor_de(linea, "cache_ram"), "4096")
        # el resto de la unidad productiva sigue ahi (vision incluida)
        for flag in ("--mmproj", "--ubatch-size", "--lazy-mode", "--metrics"):
            self.assertIn(flag, linea)

    def test_args_mtp_nmax_3_cambia_solo_el_techo_del_borrador(self):
        base = self.banco.args_de_unidad(UNIDAD_REAL)
        a = self.fases.args_mtp(base, 2, "/cabeza.gguf")
        b = self.fases.args_mtp(base, 3, "/cabeza.gguf")
        self.assertEqual(self.banco.valor_de(a, "spec_draft_n_max"), "2")
        self.assertEqual(self.banco.valor_de(b, "spec_draft_n_max"), "3")
        # el resto de la linea, identico brazo a brazo
        self.assertEqual(
            self.banco.con_cambios(a, spec_draft_n_max=None),
            self.banco.con_cambios(b, spec_draft_n_max=None))

    def test_con_cambios_conoce_los_cuatro_flags_mtp(self):
        linea = self.banco.con_cambios(
            ["--model", "x.gguf"], spec_type="draft-mtp",
            draft_model="/c.gguf", spec_draft_n_max=2, spec_draft_p_min=0.0)
        self.assertEqual(linea, ["--model", "x.gguf", "--spec-type", "draft-mtp",
                                 "-md", "/c.gguf", "--spec-draft-n-max", "2",
                                 "--spec-draft-p-min", "0.0"])

    def test_los_umbrales_estan_escritos_en_el_docstring(self):
        h34 = self.fases
        self.assertIn("1,15x", h34.__doc__)
        self.assertIn("0,95x", h34.__doc__)
        self.assertIn("0,97x", h34.__doc__)
        self.assertIn("0,90x", h34.__doc__)
        self.assertEqual((h34.UMBRAL_TG, h34.UMBRAL_TG_FAMILIA, h34.UMBRAL_PP),
                         (1.15, 0.95, 0.97))
        self.assertEqual(h34.UMBRAL_PP_ESCALERA, 0.90)


# ===========================================================================
# 2. dmesg_desde con un journalctl falso
# ===========================================================================
class VigilanciaDeKernel(unittest.TestCase):

    def setUp(self):
        self.banco = modulo("banco")
        previo = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(previo)))
        os.environ["SUDO"] = ""            # sin segundo intento por defecto

    class _R:
        def __init__(self, rc=0, stdout=""):
            self.returncode, self.stdout = rc, stdout

    def test_devuelve_las_lineas_amdgpu_y_descarta_el_resto(self):
        r = self._R(stdout="amdgpu: ring gfx_0.0.0 timeout\nfoo bar\n"
                           "kernel: amdgpu reset scheduled\n")
        lineas = self.banco.dmesg_desde("2026-01-01 00:00:00", ejecutar=lambda c: r)
        self.assertEqual(lineas, ["amdgpu: ring gfx_0.0.0 timeout",
                                  "kernel: amdgpu reset scheduled"])

    def test_sin_amdgpu_devuelve_lista_vacia_no_none(self):
        r = self._R(stdout="nada relevante\n")
        lineas = self.banco.dmesg_desde("2026-01-01 00:00:00", ejecutar=lambda c: r)
        self.assertEqual(lineas, [])       # "no ha pasado nada", NO "no lo se"

    def test_none_cuando_ningun_intento_puede_mirar(self):
        def falla(cmd):
            raise OSError("sin journalctl")
        self.assertIsNone(self.banco.dmesg_desde("2026-01-01 00:00:00",
                                                 ejecutar=falla))

    def test_prueba_sudo_como_segundo_intento(self):
        os.environ["SUDO"] = "sudo -n"
        intentos = []

        def ejecutar(cmd):
            intentos.append(cmd)
            return self._R(1) if "sudo" not in cmd else self._R(stdout="amdgpu x\n")

        lineas = self.banco.dmesg_desde("2026-01-01 00:00:00", ejecutar=ejecutar)
        self.assertEqual(lineas, ["amdgpu x"])
        self.assertEqual(len(intentos), 2)


# ===========================================================================
# 3. Fase 1: velocidad a np=1
# ===========================================================================
class VelocidadNp1(ConBanco):
    CORPUS = (2048,)

    def setUp(self):
        super().setUp()
        self.fases = modulo("fases_h034")

    def corre(self, guion):
        base = {"mtp_factor": 1.3, "acceptance": 0.66, "draft_n": 50}
        base.update(guion)
        self.b.guion(base)
        return self.fases.mtp_np1_velocidad(self.ctx())

    def test_adopta_con_1_3x_y_acceptance_0_66(self):
        r = self.corre({})
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertNotIn("error", r)
        self.assertEqual(r["resumen"]["nmax_recomendado"], 2)   # empate: n-max menor
        for brazo in ("mtp-nmax2", "mtp-nmax3"):
            b = r["resumen"]["brazos_mtp"][brazo]
            self.assertTrue(b["especulacion_activa"])
            self.assertTrue(b["por_timings"])
            self.assertTrue(b["por_log"])
            self.assertAlmostEqual(b["acceptance_mediana"], 0.66, places=2)
            self.assertAlmostEqual(b["ratio_tg"], 1.3, places=3)
        self.assertTrue(r["resumen"]["greedy"]["identico"])

    def test_alterna_control_mtp2_mtp3_por_ronda(self):
        r = self.corre({})
        self.assertEqual(r["resumen"]["orden"],
                         ["control", "mtp-nmax2", "mtp-nmax3"] * 2)
        self.assertEqual(
            [a["brazo"] for a in self.b.arranques()],
            [BASE_SHA, CAND_SHA, CAND_SHA, BASE_SHA, CAND_SHA, CAND_SHA])

    def test_no_adopta_con_1_05x(self):
        r = self.corre({"mtp_factor": 1.05})
        self.assertFalse(r["adoptar"])
        self.assertIn("por debajo de 1.15", r["error"])
        self.assertIsNone(r["resumen"]["nmax_recomendado"])

    def test_greedy_distinto_es_error_no_un_brazo_peor(self):
        r = self.corre({"mtp_greedy_distinto": True})
        self.assertFalse(r["adoptar"])
        self.assertFalse(r["resumen"]["greedy"]["identico"])
        self.assertIn("greedy", r["error"])

    def test_mtp_sin_efecto_no_se_mide_en_falso(self):
        r = self.corre({"mtp_sin_efecto": True})
        self.assertFalse(r["adoptar"])
        self.assertIn("NO esta activa", r["error"])
        for brazo in ("mtp-nmax2", "mtp-nmax3"):
            self.assertFalse(r["resumen"]["brazos_mtp"][brazo]["especulacion_activa"])

    def test_una_familia_hundida_tumba_la_adopcion(self):
        r = self.corre({"mtp_tg_por_familia": {"creativo": 0.7}})
        self.assertFalse(r["adoptar"])
        self.assertIn("creativo", r["error"])
        self.assertIn("por debajo de 0.95", r["error"])

    def test_esta_fase_no_devuelve_aplicar(self):
        self.assertNotIn("aplicar", self.corre({}))

    def test_los_brazos_mtp_arrancan_con_su_cabeza_y_su_techo(self):
        self.corre({})
        cand = [a for a in self.b.arranques() if a["brazo"] == CAND_SHA]
        self.assertEqual(len(cand), 4)      # 2 brazos MTP x 2 pasadas
        techos = {a["spec_draft_n_max"] for a in cand}
        self.assertEqual(techos, {2, 3})
        for a in cand:
            self.assertEqual(a["spec_type"], "draft-mtp")
            self.assertIn("mtp-", a["draft_model"])
        base = [a for a in self.b.arranques() if a["brazo"] == BASE_SHA]
        for a in base:
            self.assertIsNone(a["spec_type"])

    def test_las_peticiones_van_sin_cache_sin_pensar_y_con_seed_de_control(self):
        self.corre({})
        for c in self.b.cuerpos():
            self.assertIs(c["cache_prompt"], False)
            self.assertEqual(c["temperature"], 0)
            self.assertEqual(c["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(c["max_tokens"], 256)


# ===========================================================================
# 4. Fase 2: escalera de contexto
# ===========================================================================
class EscaleraContexto(ConBanco):
    CORPUS = (2048, 8192, 32768, 65536, 98304)

    def setUp(self):
        super().setUp()
        self.fases = modulo("fases_h034")
        self._dmesg_real = self.banco.dmesg_desde
        self.banco.dmesg_desde = lambda marca, ejecutar=None: []
        self.addCleanup(setattr, self.banco, "dmesg_desde", self._dmesg_real)

    def corre(self, guion=None):
        self.b.guion({"pp": 450.0, "acceptance": 0.66,
                      **(guion or {})})
        return self.fases.mtp_escalera_contexto(self.ctx())

    def test_escalera_sana_recorre_los_cuatro_puntos(self):
        r = self.corre()
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertEqual(len(r["resumen"]["escalones"]), 4)
        self.assertEqual(r["resumen"]["techo_ctx"], 98304)
        for t in ("8192", "32768", "65536", "98304"):
            esc = r["resumen"]["escalones"][t]
            self.assertTrue(esc["vigilancia_kernel"])
            self.assertFalse(esc["gpu_incidente"])
            self.assertTrue(esc["vivo"])
            self.assertTrue(esc["health_200"])
            self.assertTrue(esc["responde_corto"])
        # donde hay baseline de H-033, el pp no paga mas de un 10 %
        self.assertGreaterEqual(r["resumen"]["escalones"]["8192"]["ratio_pp"], 0.90)
        self.assertGreaterEqual(r["resumen"]["escalones"]["32768"]["ratio_pp"], 0.90)

    def test_caida_en_65536_fija_el_techo_en_32768_y_no_escala_a_98304(self):
        r = self.corre({"caida_en_ctx": 65536})
        self.assertFalse(r["adoptar"])
        self.assertEqual(r["resumen"]["techo_ctx"], 32768)
        self.assertEqual(set(r["resumen"]["escalones"]), {"8192", "32768", "65536"})
        self.assertTrue(r["resumen"]["escalones"]["65536"]["murio"])
        # la escalera se paro: nunca llego a pedir el corpus de 98304
        cuerpos = self.b.cuerpos()
        self.assertFalse(any("[[PN=98304]]" in str(c.get("messages"))
                             for c in cuerpos))

    def test_confirma_la_especulacion_antes_de_escalar(self):
        r = self.corre({})
        self.assertTrue(r["resumen"]["especulacion"]["activa"])
        self.assertTrue(r["resumen"]["especulacion"]["por_timings"])

    def test_escala_solo_el_candidato_mtp(self):
        self.corre({})
        arranques = self.b.arranques()
        self.assertEqual(len(arranques), 1)
        self.assertEqual(arranques[0]["brazo"], CAND_SHA)
        self.assertEqual(arranques[0]["spec_draft_n_max"], 2)
        self.assertEqual(arranques[0]["parallel"], 1)


class EscaleraSinCorpus(ConBanco):
    CORPUS = (2048, 8192, 32768, 98304)     # falta 65536 a proposito

    def setUp(self):
        super().setUp()
        self.fases = modulo("fases_h034")

    def test_corpus_65536_ausente_aborta_con_la_orden_de_generarlo(self):
        ErrorCorpus = modulo("prompts").ErrorCorpus
        with self.assertRaises(ErrorCorpus) as c:
            self.fases.mtp_escalera_contexto(self.ctx())
        self.assertIn("65536", str(c.exception))
        self.assertIn("generar", str(c.exception))


# ===========================================================================
# 5. Fase 3: vision
# ===========================================================================
class Vision(ConBanco):
    CORPUS = (2048,)

    def setUp(self):
        super().setUp()
        self.fases = modulo("fases_h034")

    def corre(self, guion=None):
        self.b.guion({"acceptance": 0.66, **(guion or {})})
        return self.fases.mtp_vision(self.ctx())

    def test_control_y_candidato_aciertan_y_adopta(self):
        r = self.corre()
        self.assertTrue(r["adoptar"], r.get("error"))
        for brazo in ("control", "candidato-mtp"):
            self.assertTrue(r["resumen"]["brazos"][brazo]["acierta"])
            self.assertEqual(r["resumen"]["brazos"][brazo]["texto"].lower(), "rojo")

    def test_vision_rota_con_la_cabeza_es_error_de_incompatibilidad(self):
        r = self.corre({"vision_mtp_rompe": True})
        self.assertFalse(r["adoptar"])
        self.assertIn("incompatible", r["error"])
        self.assertTrue(r["resumen"]["brazos"]["control"]["acierta"])
        self.assertFalse(r["resumen"]["brazos"]["candidato-mtp"]["acierta"])

    def test_los_dos_brazos_reciben_la_misma_imagen(self):
        self.corre({})
        cuerpos = self.b.cuerpos()
        self.assertEqual(len(cuerpos), 2)
        contenido = [c["messages"][0]["content"] for c in cuerpos]
        self.assertEqual(contenido[0], contenido[1])
        self.assertTrue(any(p.get("type") == "image_url"
                            for p in contenido[0]))

    def test_el_candidato_arranca_con_mmproj_y_cabeza(self):
        self.corre({})
        cand = [a for a in self.b.arranques() if a["brazo"] == CAND_SHA]
        self.assertEqual(len(cand), 1)
        self.assertIn("--mmproj", cand[0]["args"])
        self.assertEqual(cand[0]["spec_type"], "draft-mtp")

    def test_esta_fase_no_devuelve_aplicar(self):
        self.assertNotIn("aplicar", self.corre({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
