# H-062 — Strata frente a Gufo en el M5: Qwen y DeepSeek (2026-11-09)

Banco: `bench_vel.py` (streaming, temperatura 0, nonce por petición para que no haya caché; corpus = fuentes .py del repo de Strata en orden fijo; 2 pasadas por caso) y `bench-calidad-motores.py` (12 pruebas con verificador objetivo). Mismo script y misma sesión para los cuatro motores; sin otros trabajos en la máquina.

- `pp_tok_s` = prompt_tokens / tiempo hasta el primer token (incluye el arranque de la petición).
- `tg_tok_s` = (tokens − 1) / (fin − primer token).

Motores:
- **qwen-gufo**: producción (Gufo 0.32.0, UD-Q4_K_XL, MTP).
- **qwen-strata**: Strata v0.2.12, motor 0.1.58 compilado para gfx1151, el mismo GGUF UD-Q4_K_XL (revisión que Strata fija como buena; no el UD-IQ4_XS resubido de #5296). Configuración la que escribe `setup.sh`: `--spec 4 --mtp`, KV int8, `--resident-budget-gib 34` (el instalador recorta el presupuesto para no agotar la memoria compartida).
- **ds-gufo**: DeepSeek-V4.1-Flash, Gufo IQ1_M, fork feat/ds41 (H-057).
- **ds-strata**: DeepSeek-V4.1-Flash, Strata v0.2.12 (motor dsv41 0.1.56), pack IST-DAS 2.00bpw, DSpark, `--vram-experts 0.5`.

## Lectura de prompt (t/s, media de 2)

| Motor | ~8,6k | ~28k | ~95k |
| --- | ---: | ---: | ---: |
| qwen-gufo | 1.619 | 1.616 | 1.540 |
| qwen-strata | 384 | 482 | 464 |
| ds-gufo | 1.016 | 1.093 | 1.110 |
| ds-strata | 283 | 367 | 391 |

## Escritura (t/s)

| Motor | tras prompt largo | código 512 tok |
| --- | ---: | ---: |
| qwen-gufo | 44 | 72 |
| qwen-strata | 30-34 | 43-45 |
| ds-gufo | 23-24 | 24,5-25 |
| ds-strata | 18-23 | 25,4-26 |

## Calidad (12 pruebas)

| Motor | OK | Tiempo total | Fallos |
| --- | ---: | ---: | --- |
| qwen-gufo | 12/12 | 61 s | — |
| qwen-strata | 12/12 | 192 s | — |
| ds-gufo | 10/12 | 94 s | idioma-castellano, razonamiento-trampa |
| ds-strata | 10/12 | 291 s | idioma-castellano, razonamiento-trampa |

## Conclusión

- Gufo gana en los dos modelos. En lectura de prompt es 3-4 veces más rápido que Strata con Qwen (95k en 62 s frente a 205 s) y 2,8-3,6 veces más rápido con DeepSeek (95k en 89 s frente a 253 s).
- En escritura, Strata solo empata en código corto con DeepSeek (+1 t/s, dentro del ruido); con contexto largo pierde.
- Los dos motores de DeepSeek fallan las mismas dos pruebas, así que es cosa del modelo (o de su cuantización extrema), no del motor.
- La cifra de un vídeo (32k en unos 12 s con Strata) no se reproduce en esta máquina: aquí son 59 s.
- No cambia nada en producción: Qwen por Gufo, DeepSeek por Gufo y Strata como reserva apagada.

## Límites

- 2 pasadas por caso; las diferencias entre motores son de 3× y no dependen del ruido, pero las cifras de escritura de ±2 t/s no se interpretan.
- El presupuesto de RAM de qwen-strata lo recorta su instalador a 34 GiB; un ajuste manual (`--expert-cache auto` o un presupuesto mayor) no se ha probado. No parece capaz de cerrar una diferencia de 3× en lectura, pero es una hipótesis, no una medida.
- `codigo-512` mide la escritura con un prompt mínimo; su `pp_tok_s` no es representativo.
