# Qwen3-Next-80B-A3B-Instruct — IQ4_XS en el M5

**Modelo:** unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF, IQ4_XS, 42,6 GB (sha256 verificado contra HF).
**Arquitectura:** `qwen3next` — MoE de 80B totales / ~3B activos con atención híbrida
(capas lineales tipo gated-delta + atención completa cada pocas capas).
**Servidor:** llama-server 9113cc1, Vulkan RADV, `-ngl 99 -c 262144 -ub 2048 -b 2048 -fa on -lzm off`, puerto :8081.
**Memoria en uso con ctx 262k:** ~54 GB de 124 (el Flash-Next usa 109).

## Barrido de contexto (2 pasadas/punto + aguja, 2026-09-09)

| objetivo | prompt_n real | pp t/s | tg t/s | latencia total | aguja |
|---:|---:|---:|---:|---:|:---|
| 4.000 | 3.772 | 792,5 | 45,3* | 4,6 s | OK |
| 16.000 | 15.022 | 779,0 | 33,5 | 19,4 s | OK |
| 32.000 | 29.992 | 680,0 | 31,7 | 44,2 s | OK |
| 65.000 | 60.892 | 487,3 | 28,7 | 125 s | OK |
| 100.000 | 93.682 | 321,8 | 25,8 | 291 s | OK |
| 131.000 | 122.692 | 239,7 | 24,6 | 512 s | OK |

\* caliente; la pasada fría (primer toque de expertos) dio 9,75 y se descarta.

De 3,8k a 122,7k tokens: prefill −70%, **generación solo −11% desde 15k** (−46% si
se cuenta el pico de 45 t/s del prompt corto, dominado por caché).

## Conclusiones

- La atención híbrida hace que el coste de generación sea casi independiente del
  contexto: a 122k genera más rápido que el Flash-Next a 12k.
- 6/6 agujas: recuperación literal fiable en toda la ventana medida.
- Candidato a producción (H-013): mitad de memoria, 1,5-2x de velocidad.
  Pendiente batería de calidad A8 antes de promocionar.

Detalle y comparativa completa: [H-013](../docs/hallazgos.md).
