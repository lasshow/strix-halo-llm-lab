"""H-028: el batch debe verificarse por lo que el servidor APLICA, no por el texto.

El barrido comprobaba que el texto de la unidad contuviera --batch-size N.
Eso no demuestra que el servidor lo use: un punto medido con un batch que
nunca llego a aplicarse habria pasado por bueno y contaminado una campaña.

llama-server no expone n_batch en /props (verificado en el M5: solo n_ctx),
asi que la fuente es el paso entre trozos de 'prompt processing' del journal.

Los dos casos REALES vienen del M5 del 10-09-2026: el piloto 1 corrio con
--batch-size 2048 y el piloto 2 con 4096, y sus journals avanzan justo asi.
"""
import importlib.util
import os
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def carga():
    ruta = os.path.join(RAIZ, "scripts", "bench-ubatch.py")
    spec = importlib.util.spec_from_file_location("bench_ubatch", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def journal(paso, total=30018, extra=""):
    """Reproduce el formato real del journal de llama-server."""
    lineas = []
    n = paso
    while n < total:
        lineas.append(
            f"slot print_timing: id  1 | task 31 | prompt processing, "
            f"n_tokens = {n:6d}, progress = {n/total:.2f}, t = 7.64 s / 536.45 "
            f"tokens per second")
        n += paso
    lineas.append(
        f"slot print_timing: id  1 | task 31 | prompt processing, "
        f"n_tokens = {total:6d}, progress = 1.00, t = 82.39 s / 364.30 "
        f"tokens per second")
    return "\n".join(lineas) + extra


class TestBatchEfectivo(unittest.TestCase):
    def setUp(self):
        self.m = carga()
        self.salida = ""
        self.m.sh = lambda cmd, check=True: self.salida

    def test_detecta_4096_del_journal_real(self):
        """Piloto 2 del M5: progreso de 4096 en 4096."""
        self.salida = journal(4096)
        efec, coincide = self.m.batch_efectivo("u", "22:30", 4096)
        self.assertEqual(efec, 4096)
        self.assertTrue(coincide)

    def test_detecta_2048_del_journal_real(self):
        """Piloto 1 del M5: progreso de 2048 en 2048."""
        self.salida = journal(2048)
        efec, coincide = self.m.batch_efectivo("u", "21:30", 2048)
        self.assertEqual(efec, 2048)
        self.assertTrue(coincide)

    def test_delata_el_batch_que_no_se_aplico(self):
        """El caso que motiva todo: se pidio 4096 y el servidor uso 2048."""
        self.salida = journal(2048)
        efec, coincide = self.m.batch_efectivo("u", "21:30", 4096)
        self.assertEqual(efec, 2048)
        self.assertFalse(coincide,
                         "un batch no aplicado debe delatarse, no pasar por bueno")

    def test_ultimo_trozo_corto_no_confunde(self):
        """El resto final es menor que el batch y no debe tomarse por el batch.

        Caso real: 30018 tokens con batch 4096 deja un ultimo salto de 1346.
        Tomar el paso MINIMO devolvia 1346 y daba el punto por no comparable.
        """
        self.salida = journal(4096, total=30018)
        efec, coincide = self.m.batch_efectivo("u", "22:30", 4096)
        self.assertEqual(efec, 4096,
                         "el resto final no debe confundirse con el batch")
        self.assertTrue(coincide)

    def test_prompt_corto_no_afirma_nada(self):
        """Sin trozos suficientes se devuelve None, no una conclusion inventada."""
        self.salida = ("slot print_timing: id 1 | task 0 | prompt processing, "
                       "n_tokens =    512, progress = 1.00, t = 1.0 s / 1.0 "
                       "tokens per second")
        efec, coincide = self.m.batch_efectivo("u", "22:30", 4096)
        self.assertIsNone(efec)
        self.assertIsNone(coincide)

    def test_journal_vacio_no_afirma_nada(self):
        self.salida = ""
        efec, coincide = self.m.batch_efectivo("u", "22:30", 4096)
        self.assertIsNone(efec)
        self.assertIsNone(coincide)

    def test_no_confunde_eval_time_con_prompt_processing(self):
        """Las lineas de eval/total llevan cifras que no son trozos de prompt."""
        extra = ("\nslot print_timing: id  1 | task 31 | prompt eval time = "
                 "85909.35 ms / 30018 tokens (2.86 ms per token, 349.41 tokens "
                 "per second)\nslot print_timing: id  1 | task 31 | eval time = "
                 "996.71 ms / 23 tokens (45.30 ms per token, 22.07 tokens per "
                 "second)")
        self.salida = journal(4096) + extra
        efec, coincide = self.m.batch_efectivo("u", "22:30", 4096)
        self.assertEqual(efec, 4096)
        self.assertTrue(coincide)


if __name__ == "__main__":
    unittest.main()
