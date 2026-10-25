# H-053 — Qwen3.8-Flash-Next (Gufo) vs DeepSeek-V4.1-Flash (Strata), batería de calidad

**Fecha:** 2026-10-25 · **Estado:** cerrado (razonamiento-trampa re-verificado con CoT; agujas aceptadas por H-052; castellano = falso negativo)

Ronda A/B con la batería de 12 pruebas con verificador objetivo
(`scripts/bench-calidad-motores.py`), misma máquina, `enable_thinking: false` en
ambos motores, `temperature: 0`.

## Resultado crudo

| Motor | Resultado |
|---|---|
| Qwen3.8-Flash-Next (Gufo) | **12/12** |
| DeepSeek-V4.1-Flash (Strata) | **10/12** |

Ficheros: `cal-qwen.jsonl` y `cal-deepseek.jsonl` (una línea por prueba, con
`respuesta` truncada a 4000 caracteres y `prompt_tokens`/`completion_tokens`/`segundos`).

## Los 2 fallos de DeepSeek y su lectura honesta

**1. `idioma-castellano` — FALSO NEGATIVO del verificador (no es un fallo del modelo).**

La respuesta de DeepSeek es castellano perfecto:

> "Un PLC (Controlador Lógico Programable) es un dispositivo industrial diseñado
> para automatizar procesos. Lee señales de sensores en sus entradas, las procesa
> según un programa lógico almacenado en su memoria y activa salidas para controlar
> actuadores (como motores, válvulas o relés). Se usa ampliamente en fábricas y
> sistemas de control de procesos por su robustez, capacidad de tiempo real y …"

El verificador `v_castellano` cuenta substrings de 7 palabras función fijas
(` el `, ` la `, ` de `, ` que `, ` con `, ` para `, ` los `) y exige ≥3. Ese texto
solo contiene ` de ` y ` para ` (2 marcas) → lo marca como "no parece castellano".
Es un falso negativo: el texto es 100 % español, simplemente no usa esas palabras
concretas. **No cuenta como fallo de calidad.**

**2. `razonamiento-trampa` — fallo real, pero condicionado por el protocolo (CoT apagado).**

DeepSeek respondió `100` a "si 3 máquinas tardan 3 min en hacer 3 piezas, ¿cuánto
tardan 100 en hacer 100?" (correcto: 3). Es el fallo clásico de Sistema-1, y esta
batería corre con `enable_thinking: false`, es decir, **sin cadena de pensamiento**.
El test es precisamente de razonamiento; al quitarle el CoT a un modelo razonador
(DeepSeek V4.1 Flash) se le quita la herramienta que lo resolvería. Qwen3.8 lo pasó
igual (respondió 3) sin CoT, así que el A/B sigue siendo válido — pero el "100" no
debe leerse como "DeepSeek no razona", sino "DeepSeek **sin CoT** da la respuesta
intuitiva".

**Re-verificación con CoT activado (2026-10-25, `enable_thinking: true`, temp 0,
`max_tokens` 4096):** DeepSeek responde **`3`** (correcto), `finish_reason: stop`
(sin truncado). Tiempo total **7,11 s**; `completion_tokens` 103 (el motor no
desglosa `reasoning`/`content`; la respuesta final es `3`, 1 token, así que ~102
tokens son de razonamiento — 339 caracteres de `<think>`).

Conclusión: DeepSeek **necesita** el CoT para salvar la trampa (sin pensar daba
`100`, pensando da `3`), a costa de **7,11 s frente a los 0,3 s** del disparo
directo de Qwen, que responde `3` sin CoT. Para esta clase de pregunta, el CoT de
DeepSeek compra el acierto pagando ~24× de latencia.

## Caché en las 2 pruebas de aguja (dato contaminado)

Las dos agujas de DeepSeek salieron en **0,57 s y 0,63 s para 46.574 y 46.567
tokens** de prompt (≈ 81.000 tok/s de "prefill", imposible). Causa: el prompt de
aguja es **idéntico** al de la ronda anterior del mismo día (aguja fija
`ZX-4417-QD` + relleno fijo), y Strata lo sirve desde su caché de conversación /
reuso de prefijo, no lo vuelve a leer. **Las agujas de DeepSeek en esta ronda NO
miden lectura de contexto largo**: son un hit de caché. La capacidad real de aguja
de DeepSeek ya está verificada en H-052 (acierto a 105.474 tokens).

No afecta al marcador: las 10 pruebas cortas son genuinas (prompts únicos o sin
prefijo largo). Pero invalida el "aguja OK" de esta ronda como evidencia de contexto.

## Pendiente

- ~~Repetir `razonamiento-trampa` con CoT~~ — **hecho**, ver arriba (`3`, 7,11 s, ~102 tokens de razonamiento).
- Agujas de DeepSeek: **aceptado H-052** (aguja a 105.474 tokens OK), no se repiten.
- Heurístico `v_castellano`: **sin tocar por ahora** (decisión de Iker); queda documentado como falso negativo.
