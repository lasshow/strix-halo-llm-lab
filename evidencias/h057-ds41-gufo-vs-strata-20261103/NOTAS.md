# H-057 — DeepSeek-V4.1-Flash: Gufo IQ1_M (fork `feat/ds41`) sustituye a Strata

**Fecha:** 2026-11-03 · **Estado:** cerrado; Gufo pasa a ser el motor de DeepSeek a demanda (Qwen sigue siendo el de producción).

## Qué se compara

| | Gufo | Strata |
|---|---|---|
| Motor | `gufo` fork franzmoca, rama `feat/ds41`, commit `7549547`, build Nix en Podman | `strata-dsv41` 0.1.51 (H-052) |
| Pesos | ISTA-DASLab `GSQ-RCO-IQ1_M` (5 shards, ~112 GiB residentes) + Engram IQ4_NL (shard 6, se lee del SSD) | IST-DAS 2.00 bpw (IQ2_XXS), `--vram-experts 0.5` |
| Contexto | 131.072 | 131.072 |

Misma máquina, mismo cliente, greedy (`temperature: 0`), razonamiento apagado, 512 tokens,
3 pasadas por prompt con la **primera (fría) descartada**. Cliente:
[`scripts/h057-cliente-stream.py`](../../scripts/h057-cliente-stream.py) (streaming; mide la
generación entre el primer y el último token).

## Velocidad (`resultados.jsonl`)

| Prompt | Tokens de prompt | Gufo tg (t/s) | Strata tg (t/s) |
|---|---|---|---|
| prosa (cuento) | 28 | **24,6** (24,63 / 24,60) | 20,2 (19,75 / 20,58) |
| código | 54 | 24,5 (24,48 / 24,54) | **25,7** (25,57 / 25,80) |
| edición de un script (devolverlo con un cambio) | 2.412 | **66,8** (66,80 / 66,81) | 28,7 (28,38 / 29,00) |

- En **edición**, Gufo acelera **sin DSpark**: lo hace el borrador por *prompt lookup*. El log del
  servidor lo prueba: `draft_accepted=452 draft_proposed=462 acceptance_pct=97.8` (`gufo-plain.log`).
- **Primer token en frío del prompt de 2.412 tokens:** Gufo 2,65 s frente a Strata 16,52 s (≈ ×6).
- **Prefill largo real** (agujas de la batería con `--bust-cache`, 46.586 tokens):
  Gufo 36,6 s (≈ 1.270 t/s) frente a Strata 117,9 s (≈ 395 t/s) → **×3,2**.

## Calidad: batería de 12 pruebas con verificador objetivo

`scripts/bench-calidad-motores.py --bust-cache`, misma ronda, mismo verificador:

| Motor | Resultado | Fallos |
|---|---|---|
| Gufo IQ1_M | **12/12** | — |
| Strata IQ2_XXS | 11/12 | `razonamiento-trampa`: responde `100` sin CoT (igual que en H-053) |

Ficheros: `cal-gufo-iq1m.jsonl`, `cal-strata.jsonl`. Ojo: 12 pruebas no bastan para decir
que IQ1_M (1,75 bpw) sea *mejor* que IQ2_XXS. Lo que se puede afirmar es que **no se ve
pérdida** con esta batería. Las cifras del autor (MMLU-Pro 74,3 frente a 75,4) no se han
reproducido aquí.

## DSpark (borrador especulativo): no cabe en este M5

```
ds41: the model needs 118.59 GiB (112.00 GiB weights, 4.59 GiB drafter, 2 GiB for sessions)
but 116.84 GiB of memory is available
```

- Probado con `--context 65536` y `32768` + `--cache-ram-bytes off`: **el bloque de sesiones
  sigue siendo de 2 GiB** y el requisito no baja de 118,59 GiB (`campana3.log`).
- Con todo parado salvo los servicios de base del host, quedan 116,8 GiB. El autor tiene
  ~1,75 GiB libres *después* de cargar con DSpark: su host deja ~3,5 GiB más que este.
- Alternativa no probada: `--stream-experts` (expertos desde el SSD, más lento; este M5 lleva
  un NVMe más lento que el del autor).
- Sin DSpark, la escritura libre queda en ~24,5 t/s. La edición ya va a ~67 t/s por *prompt lookup*.

## Servicio y conmutación (aptitud para uso real)

Unidad [`scripts/llama-ds41.service`](../../scripts/llama-ds41.service): contenedor rootless,
**mismo puerto (:8085), mismo nombre de modelo (`deepseek-v4.1-flash`) y misma clave** que
Strata, así que los clientes (Hermes, opencode, el vigía) no cambian nada. `m5-model deepseek`
arranca Gufo; `m5-model strata` queda como reserva.

E2E desde otro equipo de la red ([`scripts/h057-e2e-servicio.py`](../../scripts/h057-e2e-servicio.py), `e2e-gufo.txt`): **7/7**
- nombre de modelo, 401 sin clave y con clave falsa;
- chat corto del vigía (`finish_reason: stop`, 1,1 s);
- con razonamiento: trampa de las 3 máquinas → `3` (1.732 caracteres de razonamiento, 18,3 s);
- tool calling y vuelta del resultado de la herramienta;
- sin fuga de contexto entre dos peticiones seguidas.

Conmutación (`conmuta2.out`): 7 cambios seguidos `strata → qwen → strata → deepseek → strata →
deepseek → qwen`, todos OK en **18-23 s** cada uno, y `llama-guard` vuelve a `activo` al final.

## Dos trampas que salieron al integrarlo

1. **Gufo pide la clave también en `/health`** (ya visto en H-051 con Qwen). El `ds_health` de
   `m5-model` hacía `curl` sin clave → 401 → esperaba 10 min y daba FALLO con el servicio sano.
   Ahora manda la clave (Strata la ignora).
2. **`stop_ds41` esperaba a que se liberara el :8085 aunque la unidad estuviera parada.** Si el
   puerto era de Strata, `m5-model qwen` fallaba en 20 s. Ahora solo espera al puerto si la
   unidad estaba viva.

Además, `llama-guard` ya no levanta Qwen mientras `llama-ds41` esté activo, aunque falte el
centinela. Comprobado quitando el centinela a mano con Gufo-DS4.1 sirviendo.

## Memoria y límites

- Sirviendo, al host le quedan **~0,9-1,0 GiB** (`host_available_mib` en el log). No convive
  con nada que use la GPU (Qwen, brush, ComfyUI con trabajo).
- Una sesión a la vez (igual que Strata).
- El fichero Engram compartido de Strata (layout anterior de 4 shards) **no vale** para Gufo:
  aborta con `Inconsistent split GGUF shard metadata`. Gufo usa su propia copia del Engram del
  repo republicado de ISTA (103 GiB más en disco).
