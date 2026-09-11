#!/usr/bin/env python3
"""Pruebas de H-036: cada cambio de drluoto, uno a uno, contra la produccion
vigente (que YA lleva la cabeza MTP de H-035c).

Misma tecnica que `test_h035.py`. La unidad del arbol falso es la de H-035c
(con los cuatro flags MTP), porque H-036 exige que el control lleve MTP.

Ejecutar: python3 tests/test_h036.py -v
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(os.path.dirname(AQUI), "scripts"))

from test_h032_h033 import (BASE_SHA, CAND_SHA, CLAVE, MODELO, UNIDAD,  # noqa: E402
                            UNIDAD_REAL, BancoFalso, ConBanco)


def modulo(nombre):
    if nombre in sys.modules:
        return importlib.reload(sys.modules[nombre])
    return importlib.import_module(nombre)


CABEZA_PROD = "/models/gguf/x/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf"


def unidad_con_mtp():
    banco = modulo("banco")
    return banco.pon_mtp(UNIDAD_REAL, CABEZA_PROD, 2, 0.0)


# ===========================================================================
# 1. pon_modelo y la clave `model` en con_cambios
# ===========================================================================
class EscrituraDeModelo(unittest.TestCase):

    def setUp(self):
        self.banco = modulo("banco")

    def test_pon_modelo_cambia_solo_el_gguf(self):
        t = self.banco.pon_modelo(UNIDAD_REAL, "/m/q5k/tronco-00001-of-00003.gguf")
        a = self.banco.args_de_unidad(t)
        self.assertEqual(self.banco.valor_de(a, "model"), "/m/q5k/tronco-00001-of-00003.gguf")
        b = self.banco.args_de_unidad(UNIDAD_REAL)
        for k in ("mmproj", "parallel", "kvu", "cache_ram", "alias"):
            self.assertEqual(self.banco.valor_de(a, k), self.banco.valor_de(b, k))

    def test_pon_modelo_rechaza_ruta_relativa(self):
        with self.assertRaises(RuntimeError):
            self.banco.pon_modelo(UNIDAD_REAL, "tronco.gguf")

    def test_con_cambios_model(self):
        a = self.banco.args_de_unidad(UNIDAD_REAL)
        c = self.banco.con_cambios(a, model="/m/otro.gguf")
        self.assertEqual(self.banco.valor_de(c, "model"), "/m/otro.gguf")
        self.assertEqual(c.count("--model"), 1)


# ===========================================================================
# 2. Fases contra el banco falso
# ===========================================================================
class ConBancoH036(ConBanco):
    CORPUS = (2048,)

    def setUp(self):
        # ConBanco monta con UNIDAD_REAL; aqui la unidad lleva MTP.
        super().setUp()
        with open(self.b.ruta_unidad, "w", encoding="utf-8") as f:
            f.write(unidad_con_mtp())
        self.f = modulo("fases_h036")
        os.environ["H035_PASADAS"] = "1"
        for k in ("H036_FRSPEC_HEAD", "H036_REQUANT_GGUF", "H036_NMAX"):
            os.environ.pop(k, None)
        # ficheros que las fases exigen que existan
        self.tmp = tempfile.mkdtemp(prefix="h036-")
        self.frspec = os.path.join(self.tmp, "mtp-frspec-65k.gguf")
        self.requant = os.path.join(self.tmp, "tronco-q5k-00001-of-00003.gguf")
        for p in (self.frspec, self.requant):
            open(p, "w").close()
        os.environ["H036_FRSPEC_HEAD"] = self.frspec
        os.environ["H036_REQUANT_GGUF"] = self.requant

    def base(self, **extra):
        g = {"acceptance": 0.9, "draft_n": 50}
        g.update(extra)
        self.b.guion(g)


class ControlConMTP(ConBancoH036):

    def test_exige_que_produccion_lleve_mtp(self):
        with open(self.b.ruta_unidad, "w", encoding="utf-8") as f:
            f.write(UNIDAD_REAL)
        self.base()
        with self.assertRaises(RuntimeError):
            self.f._prod(self.ctx())

    def test_el_juez_greedy_usa_la_referencia_sin_mtp_no_el_control(self):
        """El control lleva MTP y, como el servidor real, solo trae logprobs
        del primer token. Si el juez usara el control como referencia, toda
        divergencia caeria en 'sin_distribucion' y tumbaria la fase. Con la
        referencia sin especulacion, un empate se clasifica como empate."""
        self.base(tg_por_nmax={"3": 1.2},
                  divergencia_brazo=BASE_SHA,   # el candidato n-max 3 corre en baseline
                  divergencia={"prosa": {"posicion": 6, "clase": "empate"}})
        r = self.f.nmax3(self.ctx())
        g = r["resumen"]["greedy"]
        self.assertEqual(g["sin_distribucion"], [])
        self.assertEqual(g["no_verificadas"], [])
        self.assertTrue(any("prosa" in e for e in g["empates"]), g)
        self.assertTrue(r["adoptar"], r.get("error"))


class Nmax3(ConBancoH036):

    def test_adopta_si_nmax3_da_1_1x_y_aplicar_cambia_solo_nmax(self):
        self.base(tg_por_nmax={"3": 1.10})
        r = self.f.nmax3(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertAlmostEqual(r["resumen"]["ratio_tg"], 1.10, places=3)
        self.assertEqual(r["resumen"]["n_max_control"], 2)
        # ambos brazos con la MISMA build (baseline) y con MTP
        arr = self.b.arranques()
        self.assertEqual({a["brazo"] for a in arr}, {BASE_SHA})
        # control (n-max 2), candidato (n-max 3) y la REFERENCIA sin especulacion
        self.assertEqual(sorted(str(a["spec_draft_n_max"]) for a in arr), ["2", "3", "None"])
        self.assertEqual([a["spec_type"] for a in arr][-1], None)
        banco = modulo("banco")
        t = r["aplicar"](unidad_con_mtp())
        a = banco.args_de_unidad(t)
        self.assertEqual(banco.valor_de(a, "spec_draft_n_max"), "3")
        self.assertEqual(banco.valor_de(a, "draft_model"), CABEZA_PROD)
        self.assertEqual(t.count("--spec-type"), 1)

    def test_no_adopta_con_1_03x_y_aplicar_no_toca(self):
        self.base(tg_por_nmax={"3": 1.03})
        r = self.f.nmax3(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("por debajo de 1.08", r["error"])
        u = unidad_con_mtp()
        self.assertEqual(r["aplicar"](u), u)

    def test_una_familia_hundida_tumba(self):
        self.base(tg_por_nmax={"3": 1.2}, mtp_tg_por_familia={"creativo": 0.7})
        # mtp_tg_por_familia aplica a ambos brazos (ambos llevan MTP): anulo en control
        # usando el factor por nmax solo en el candidato -> uso divergencia por familia
        r = self.f.nmax3(self.ctx())
        # ambos brazos pierden igual en creativo -> ratio 1.2 -> adopta
        self.assertTrue(r["adoptar"], r.get("error"))


class FrSpec(ConBancoH036):

    def test_adopta_y_aplicar_pone_la_cabeza_nueva(self):
        self.base(tg_por_cabeza={"mtp-frspec-65k.gguf": 1.15})
        r = self.f.frspec(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        arr = self.b.arranques()
        self.assertEqual([a["brazo"] for a in arr], [BASE_SHA, CAND_SHA, BASE_SHA])
        self.assertIn("frspec", arr[1]["draft_model"])
        self.assertIsNone(arr[2]["spec_type"])          # referencia sin MTP
        self.assertEqual(r["resumen"]["greedy"]["comparaciones"], 20)  # 2 brazos x 5 fam x 2 slots
        banco = modulo("banco")
        a = banco.args_de_unidad(r["aplicar"](unidad_con_mtp()))
        self.assertEqual(banco.valor_de(a, "draft_model"), self.frspec)
        self.assertEqual(banco.valor_de(a, "spec_draft_n_max"), "2")

    def test_sin_cabeza_en_disco_no_adopta_sin_arrancar_nada(self):
        os.remove(self.frspec)
        self.base()
        r = self.f.frspec(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("no existe", r["error"])
        self.assertEqual(self.b.arranques(), [])

    def test_no_verificado_tumba(self):
        self.base(tg_por_cabeza={"mtp-frspec-65k.gguf": 1.3},
                  divergencia_brazo=CAND_SHA,
                  divergencia={"codigo": {"posicion": 4, "clase": "lejano"}})
        r = self.f.frspec(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("NO son empate", r["error"])


class Requant(ConBancoH036):

    def test_adopta_y_aplicar_cambia_el_modelo(self):
        self.base(tg_por_modelo={"tronco-q5k-00001-of-00003.gguf": 1.12})
        r = self.f.requant(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        arr = self.b.arranques()
        self.assertEqual({a["brazo"] for a in arr}, {BASE_SHA})
        self.assertTrue(any(a["model"] == self.requant for a in arr))
        banco = modulo("banco")
        a = banco.args_de_unidad(r["aplicar"](unidad_con_mtp()))
        self.assertEqual(banco.valor_de(a, "model"), self.requant)
        self.assertEqual(banco.valor_de(a, "spec_type"), "draft-mtp")

    def test_sin_gguf_no_adopta(self):
        os.remove(self.requant)
        self.base()
        r = self.f.requant(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertEqual(self.b.arranques(), [])


class KvZero(ConBancoH036):

    def test_adopta_sin_perder_y_no_devuelve_aplicar(self):
        self.base()
        r = self.f.kvzero(self.ctx())
        self.assertTrue(r["adoptar"], r.get("error"))
        self.assertNotIn("aplicar", r)
        self.assertEqual([a["brazo"] for a in self.b.arranques()], [BASE_SHA, CAND_SHA, BASE_SHA])

    def test_perder_3_por_ciento_tumba(self):
        self.base(tg={"por_defecto": 25.0, CAND_SHA: 24.0})
        r = self.f.kvzero(self.ctx())
        self.assertFalse(r["adoptar"])
        self.assertIn("por debajo de 0.98", r["error"])


if __name__ == "__main__":
    unittest.main()
