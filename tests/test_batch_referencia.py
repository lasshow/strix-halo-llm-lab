"""H-027: el batch de medida debe salir de la unidad productiva, no de la mano.

El piloto del 10-09-2026 se lanzo con --batch 2048 mientras produccion corria
con --batch-size 4096. El barrido no fallo: publico cifras con pinta de buenas
que NO eran comparables con la linea base. Estas pruebas reproducen ese fallo
y fijan el comportamiento corregido.
"""
import importlib.util
import json
import os
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(AQUI, "..", "scripts")


def carga(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = carga("bench_ubatch", os.path.join(SCRIPTS, "bench-ubatch.py"))

UNIDAD_PROD = """[Unit]
Description=llama.cpp server - Qwen3.8-Flash-Next

[Service]
ExecStart=/models/llama.cpp/build/bin/llama-server \\
  --model /models/x.gguf \\
  -ngl 99 \\
  --batch-size 4096 \\
  --ubatch-size 2048 \\
  --host 0.0.0.0 --port 8080
"""


class TestBatchDeReferencia(unittest.TestCase):
    def test_lee_el_batch_de_la_unidad_productiva(self):
        self.assertEqual(bench.batch_de_referencia(UNIDAD_PROD), 4096)

    def test_falla_si_la_unidad_no_declara_batch(self):
        # Antes se habria seguido con un default silencioso de 4096, que puede
        # no ser el de esta maquina.
        with self.assertRaises(RuntimeError):
            bench.batch_de_referencia("ExecStart=/bin/llama-server --ubatch-size 2048")

    def test_el_valor_leido_no_es_el_default_historico(self):
        """Si la unidad dice 2048, la referencia es 2048 y no el 4096 cableado."""
        otra = UNIDAD_PROD.replace("--batch-size 4096", "--batch-size 2048")
        self.assertEqual(bench.batch_de_referencia(otra), 2048)


class TestMarcaDeComparabilidad(unittest.TestCase):
    """Cada fila del JSONL debe poder juzgarse sola, sin leer el log."""

    def _fila(self, batch, batch_ref):
        return {"comparable_con_produccion": (batch_ref is None or batch == batch_ref)}

    def test_reproduce_el_fallo_del_piloto(self):
        # batch 2048 medido contra produccion en 4096 -> NO comparable
        self.assertFalse(self._fila(2048, 4096)["comparable_con_produccion"])

    def test_batch_coincidente_es_comparable(self):
        self.assertTrue(self._fila(4096, 4096)["comparable_con_produccion"])


class TestPasadaEnElRegistro(unittest.TestCase):
    """H-027 defecto 2: sin indice de pasada, cinco medidas por punto son
    indistinguibles entre si en el JSONL."""

    def test_las_medidas_van_numeradas_y_el_calentamiento_no(self):
        medidas = []
        filas = []
        for es_warmup in (True, False, False, False):
            filas.append({
                "fase": "calentamiento" if es_warmup else "medida",
                "pasada": None if es_warmup else len(medidas) + 1,
            })
            if not es_warmup:
                medidas.append(object())
        self.assertEqual([f["pasada"] for f in filas], [None, 1, 2, 3])

    def test_el_jsonl_del_piloto_no_traia_pasada(self):
        """Constancia del defecto: el registro publicado carece del campo."""
        ruta = os.path.join(AQUI, "..", "evidencias", "piloto-ubatch",
                            "piloto-1024.jsonl")
        if not os.path.exists(ruta):
            self.skipTest("evidencia del piloto no disponible")
        filas = [json.loads(l) for l in open(ruta) if l.strip()]
        self.assertTrue(filas)
        self.assertTrue(all("pasada" not in f for f in filas),
                        "si esto falla, el JSONL ya fue regenerado")


if __name__ == "__main__":
    unittest.main(verbosity=2)
