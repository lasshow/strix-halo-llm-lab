#!/usr/bin/env python3
"""Pruebas de la fase H-035: MTP sidecar en la linea productiva (`np=2 -kvu`).

Misma tecnica que `test_h034.py`: `tests/banco_falso.py` hace de llama-server,
con guion por JSON. Aqui el fake sabe ademas devolver `logprobs` con una
divergencia colocada en una posicion concreta y de una clase concreta
(empate / no_verificado / lejano), y contaminar entre slots SOLO cuando el
arranque lleva la cabeza MTP (#28286).

Ejecutar: python3 tests/test_h035.py -v
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(os.path.dirname(AQUI), "scripts"))

from test_h032_h033 import (BASE_SHA, CAND_SHA, CLAVE, MODELO, UNIDAD,  # noqa: E402
                            UNIDAD_REAL, ConBanco)


def modulo(nombre):
    if nombre in sys.modules:
        return importlib.reload(sys.modules[nombre])
    return importlib.import_module(nombre)


# ===========================================================================
# 1. Escritura de la unidad: pon_mtp / quita_mtp
# ===========================================================================
class EscrituraDeUnidad(unittest.TestCase):

    def setUp(self):
        self.banco = modulo("banco")
        self.cabeza = "/models/gguf/x/MTP/mtp-x.gguf"

    def test_pon_mtp_inserta_los_cuatro_flags_tras_mmproj(self):
        texto = self.banco.pon_mtp(UNIDAD_REAL, self.cabeza, 2)
        args = self.banco.args_de_unidad(texto)
        self.assertEqual(self.banco.valor_de(args, "spec_type"), "draft-mtp")
        self.assertEqual(self.banco.valor_de(args, "draft_model"), self.cabeza)
        self.assertEqual(self.banco.valor_de(args, "spec_draft_n_max"), "2")
        self.assertEqual(self.banco.valor_de(args, "spec_draft_p_min"), "0")
        i_mm = args.index("--mmproj")
        self.assertEqual(args[i_mm + 2], "--spec-type")
        # el resto de la linea productiva no se toca
        for clave in ("parallel", "kvu", "cache_ram", "mmproj", "api_key_file"):
            self.assertEqual(self.banco.valor_de(args, clave),
                             self.banco.valor_de(self.banco.args_de_unidad(UNIDAD_REAL), clave))

    def test_pon_mtp_es_idempotente_y_no_duplica(self):
        una = self.banco.pon_mtp(UNIDAD_REAL, self.cabeza, 2)
        dos = self.banco.pon_mtp(una, self.cabeza, 3)
        self.assertEqual(dos.count("--spec-type"), 1)
        self.assertEqual(dos.count("-md "), 1)
        self.assertEqual(self.banco.valor_de(self.banco.args_de_unidad(dos),
                                             "spec_draft_n_max"), "3")

    def test_quita_mtp_devuelve_la_unidad_original(self):
        con = self.banco.pon_mtp(UNIDAD_REAL, self.cabeza, 2)
        self.assertEqual(self.banco.quita_mtp(con), UNIDAD_REAL)
        self.assertEqual(self.banco.quita_mtp(UNIDAD_REAL), UNIDAD_REAL)

    def test_pon_mtp_sin_mmproj_es_error(self):
        sin = "\n".join(l for l in UNIDAD_REAL.splitlines(True) if "--mmproj" not in l)
        with self.assertRaises(RuntimeError):
            self.banco.pon_mtp(sin, self.cabeza, 2)

    def test_pon_mtp_rechaza_cabeza_relativa_y_nmax_invalido(self):
        with self.assertRaises(RuntimeError):
            self.banco.pon_mtp(UNIDAD_REAL, "MTP/mtp.gguf", 2)
        with self.assertRaises(RuntimeError):
            self.banco.pon_mtp(UNIDAD_REAL, self.cabeza, 0)


# ===========================================================================
# 2. tokens_con_logprobs y clasificacion de divergencias (sin servidor)
# ===========================================================================
class Clasificacion(unittest.TestCase):

    def setUp(self):
        self.val = modulo("validacion")
        self.f = modulo("fases_h035")

    def _seq(self, toks, top_extra=None):
        return [{"token": t, "logprob": -0.1,
                 "top": dict({t: -0.1, t + "_alt": -0.4, t + "_lejos": -3.1},
                             **(top_extra or {}))} for t in toks]

    def test_identico(self):
        a = self._seq(["a", " b", " c"])
        self.assertEqual(self.f.clasifica_divergencia(a, a, 0.5)["clase"], "identico")

    def test_empate_dentro_del_margen(self):
        c = self._seq(["a", " b", " c"])
        m = self._seq(["a", " b_alt", " c"])
        r = self.f.clasifica_divergencia(c, m, 0.5)
        self.assertEqual(r["clase"], "empate")
        self.assertEqual(r["posicion"], 1)
        self.assertAlmostEqual(r["distancia_nats"], 0.3)

    def test_mismo_token_fuera_del_margen_es_no_verificado(self):
        c = self._seq(["a", " b", " c"])
        m = self._seq(["a", " b_lejos", " c"])
        r = self.f.clasifica_divergencia(c, m, 0.5)
        self.assertEqual(r["clase"], "no_verificado")
        self.assertAlmostEqual(r["distancia_nats"], 3.0)

    def test_token_fuera_del_top_es_no_verificado(self):
        c = self._seq(["a", " b", " c"])
        m = self._seq(["a", " zz", " c"])
        r = self.f.clasifica_divergencia(c, m, 0.5)
        self.assertEqual(r["clase"], "no_verificado")
        self.assertIsNone(r["logprob_control_del_token_mtp"])

    def test_longitud_distinta_con_prefijo_igual(self):
        c = self._seq(["a", " b", " c"])
        m = self._seq(["a", " b"])
        r = self.f.clasifica_divergencia(c, m, 0.5)
        self.assertEqual(r["clase"], "longitud")
        self.assertEqual(r["posicion"], 2)

    def test_tokens_con_logprobs_exige_el_bloque(self):
        with self.assertRaises(self.val.ErrorInfraestructura):
            self.val.tokens_con_logprobs(
                {"choices": [{"message": {"content": "x"}}]})
        with self.assertRaises(self.val.ErrorInfraestructura):
            self.val.tokens_con_logprobs(
                {"choices": [{"message": {"content": "x"}, "logprobs": {"content": []}}]})
        r = self.val.tokens_con_logprobs(
            {"choices": [{"logprobs": {"content": [
                {"token": "a", "logprob": -0.2,
                 "top_logprobs": [{"token": "a", "logprob": -0.2},
                                  {"token": "b", "logprob": -1.0}]}]}}]})
        self.assertEqual(r[0]["top"], {"a": -0.2, "b": -1.0})


# ===========================================================================
# 3. Linea de arranque productiva + MTP
# ===========================================================================
class LineaProductivaMTP(unittest.TestCase):

    def setUp(self):
        self.banco = modulo("banco")
        self.f = modulo("fases_h035")

    def test_args_prod_mtp_conserva_np_kvu_y_cache_ram(self):
        base = self.banco.args_de_unidad(UNIDAD_REAL)
        a = self.f.args_prod_mtp(base, 2, "/m/mtp.gguf")
        self.assertEqual(self.banco.valor_de(a, "parallel"), self.banco.valor_de(base, "parallel"))
        self.assertEqual(self.banco.valor_de(a, "kvu"), self.banco.valor_de(base, "kvu"))
        self.assertEqual(self.banco.valor_de(a, "cache_ram"), self.banco.valor_de(base, "cache_ram"))
        self.assertEqual(self.banco.valor_de(a, "spec_type"), "draft-mtp")
        self.assertEqual(self.banco.valor_de(a, "draft_model"), "/m/mtp.gguf")
        self.assertEqual(self.banco.valor_de(a, "spec_draft_n_max"), "2")


# ===========================================================================
# 4. Fases contra el banco falso
# ===========================================================================
class ConBancoH035(ConBanco):
    CORPUS = (2048,)

    def setUp(self):
        super().setUp()
        self.f = modulo("fases_h035")
        os.environ["H035_CICLOS"] = "6"
        os.environ["H035_PASADAS"] = "1"
        for k in ("H035_NMAX", "H035_MARGEN", "H035_MTP_HEAD"):
            os.environ.pop(k, None)

    def base(self, **extra):
        g = {"mtp_factor": 1.3, "acceptance": 0.66, "draft_n": 50}
        g.update(extra)
        self.b.guion(g)


class Diagnostico(ConBancoH035):

    def test_sin_divergencias_adopta_y_guarda_content_completo(self):
        self.base()
        ctx = self.ctx()
        r = self.f.diagnostico_greedy(ctx)
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertEqual(r["resumen"]["n_identicas"], 5)
        self.assertEqual(ctx.familias_empate_h035, set())
        for d in r["resumen"]["divergencias"].values():
            self.assertGreater(len(d["content_control"]), 80)
        # las peticiones pidieron logprobs
        for c in self.b.cuerpos():
            self.assertIs(c["logprobs"], True)
            self.assertEqual(c["top_logprobs"], 5)

    def test_empate_adopta_y_lo_explica(self):
        self.base(divergencia={"prosa": {"posicion": 7, "clase": "empate"}})
        ctx = self.ctx()
        r = self.f.diagnostico_greedy(ctx)
        self.assertTrue(r["adoptar"], r.get("error"))
        d = r["resumen"]["divergencias"]["prosa"]
        self.assertEqual(d["clase"], "empate")
        self.assertEqual(d["posicion"], 7)
        self.assertIn("diff", d)
        self.assertNotEqual(d["content_control"], d["content_mtp"])
        self.assertEqual(ctx.familias_empate_h035, {"prosa"})

    def test_no_verificado_tumba_la_fase(self):
        self.base(divergencia={"codigo": {"posicion": 3, "clase": "no_verificado"},
                               "prosa": {"posicion": 7, "clase": "empate"}})
        ctx = self.ctx()
        r = self.f.diagnostico_greedy(ctx)
        self.assertFalse(r["adoptar"])
        self.assertIn("codigo", r["error"])
        self.assertIn("no elegia", r["error"])
        self.assertEqual(r["resumen"]["n_no_verificadas"], 1)
        self.assertEqual(r["resumen"]["n_empates"], 1)
        self.assertEqual(ctx.familias_empate_h035, set())   # sin adopcion no se explica nada

    def test_distancia_mayor_que_el_margen_es_no_verificado(self):
        self.base(divergencia={"json": {"posicion": 2, "clase": "lejano"}})
        r = self.f.diagnostico_greedy(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertEqual(r["resumen"]["clases"]["json"], "no_verificado")

    def test_sin_logprobs_es_error_de_infraestructura_no_identico(self):
        self.base(sin_logprobs=True)
        r = self.f.diagnostico_greedy(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("logprobs", r["error"])
        self.assertEqual(r["resumen"]["n_identicas"], 0)

    def test_la_fase_corre_a_np1_sin_kvu(self):
        self.base()
        self.f.diagnostico_greedy(self.ctx())
        for a in self.b.arranques():
            self.assertEqual(a["parallel"], 1)
            self.assertFalse(a["kvu"])


class Aislamiento(ConBancoH035):

    def test_sin_fuga_adopta(self):
        self.base()
        r = self.f.np2_kvu_aislamiento(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertEqual(r["resumen"]["contaminaciones"], 0)
        self.assertEqual(r["resumen"]["peticiones"], 12)
        self.assertTrue(r["resumen"]["especulacion"]["timings"])

    def test_fuga_solo_con_mtp_tumba_la_fase(self):
        self.base(contamina_con_mtp=True)
        r = self.f.np2_kvu_aislamiento(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertGreater(r["resumen"]["contaminaciones"], 0)
        self.assertIn("28286", r["error"])

    def test_arranca_con_la_linea_productiva_mas_cabeza(self):
        self.base()
        self.f.np2_kvu_aislamiento(self.ctx())
        a = self.b.arranques()[0]
        self.assertEqual(a["brazo"], CAND_SHA)
        self.assertEqual(a["parallel"], 2)
        self.assertTrue(a["kvu"])
        self.assertEqual(a["spec_type"], "draft-mtp")
        self.assertEqual(a["spec_draft_n_max"], 2)

    def test_mtp_sin_efecto_no_se_da_por_aislado(self):
        self.base(mtp_sin_efecto=True)
        r = self.f.np2_kvu_aislamiento(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("NO esta activa", r["error"])


class Velocidad(ConBancoH035):

    def test_adopta_con_1_3x_y_aplicar_pone_mtp(self):
        self.base()
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertAlmostEqual(r["resumen"]["ratio_tg"], 1.3, places=3)
        self.assertEqual(r["resumen"]["concurrencia"], 2)
        for f in self.f.FAMILIAS:
            self.assertEqual(r["resumen"]["tabla"]["mtp"][f]["muestras"], 2)
        texto = r["aplicar"](UNIDAD_REAL)
        banco = modulo("banco")
        args = banco.args_de_unidad(texto)
        self.assertEqual(banco.valor_de(args, "spec_type"), "draft-mtp")
        self.assertEqual(banco.valor_de(args, "spec_draft_n_max"), "2")
        self.assertEqual(banco.valor_de(args, "parallel"), "2")

    def test_no_adopta_y_aplicar_deja_la_unidad_sin_mtp(self):
        self.base(mtp_factor=1.05)
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertEqual(r["aplicar"](UNIDAD_REAL), UNIDAD_REAL)
        banco = modulo("banco")
        con = banco.pon_mtp(UNIDAD_REAL, "/m/mtp.gguf", 2)
        self.assertEqual(r["aplicar"](con), UNIDAD_REAL)

    def test_greedy_distinto_sin_logprobs_que_lo_expliquen_es_fallo(self):
        """MTP elige en `codigo` un token que esta en el top del control pero
        a 3 nats: la referencia no lo consideraba de verdad -> no_verificado."""
        self.base(divergencia={"codigo": {"posicion": 4, "clase": "lejano"}})
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("NO son empate", r["error"])
        self.assertTrue(any("mtp-vs-control codigo" in e
                            for e in r["resumen"]["greedy"]["no_verificadas"]))

    def test_empate_intra_slot_y_mtp_control_pasa(self):
        """Lo que hace el M5 a np=2: los dos slots divergen entre si (tambien
        en el control) y MTP diverge del control, todo a <0,5 nats -> pasa."""
        self.base(divergencia_intra_slot={"posicion": 5, "clase": "empate"},
                  divergencia={"prosa": {"posicion": 9, "clase": "empate"}})
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        g = r["resumen"]["greedy"]
        self.assertGreater(len(g["empates"]), 0)
        self.assertEqual(g["no_verificadas"], [])
        self.assertTrue(any("slot0-vs-slot1" in e for e in g["empates"]))
        self.assertTrue(any("mtp-vs-control prosa" in e for e in g["empates"]))

    def test_divergencia_intra_slot_no_verificada_tumba(self):
        self.base(divergencia_intra_slot={"posicion": 5, "clase": "no_verificado"})
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertTrue(any("slot0-vs-slot1" in e
                            for e in r["resumen"]["greedy"]["no_verificadas"]))

    def test_las_peticiones_de_velocidad_piden_logprobs(self):
        self.base()
        self.f.np2_kvu_velocidad(self.ctx())
        for c in self.b.cuerpos():
            self.assertIs(c["logprobs"], True)
            self.assertEqual(c["top_logprobs"], 5)

    def test_sin_logprobs_la_fase_no_adopta(self):
        self.base(sin_logprobs=True)
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("logprobs", r["error"])

    def test_alterna_control_y_mtp_con_la_linea_productiva(self):
        os.environ["H035_PASADAS"] = "2"
        self.base()
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertEqual(r["resumen"]["orden"], ["control", "mtp", "control", "mtp"])
        for a in self.b.arranques():
            self.assertEqual(a["parallel"], 2)
            self.assertTrue(a["kvu"])
        self.assertEqual([a["brazo"] for a in self.b.arranques()],
                         [BASE_SHA, CAND_SHA, BASE_SHA, CAND_SHA])

    def test_una_familia_hundida_tumba(self):
        self.base(mtp_tg_por_familia={"creativo": 0.7})
        r = self.f.np2_kvu_velocidad(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("creativo", r["error"])


class Vision(ConBancoH035):

    def test_adopta_con_vision_y_texto_concurrentes(self):
        self.base()
        r = self.f.np2_kvu_vision(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        c = r["resumen"]["brazos"]["mtp_concurrente"]
        self.assertTrue(c["vision_acierta"])
        self.assertTrue(c["texto_acierta"])
        self.assertNotIn("aplicar", r)

    def test_vision_rota_con_mtp_tumba(self):
        self.base(vision_mtp_rompe=True)
        r = self.f.np2_kvu_vision(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("mtp", r["error"])


if __name__ == "__main__":
    unittest.main()
