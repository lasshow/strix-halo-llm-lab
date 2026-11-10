# H-063 — ¿Hay margen en la configuración de Gufo? DeepSeek con Strata v0.2.13.1 (2026-11-10)

Ventana 06:49-07:10. Gufo 0.33.0 (imagen de producción). Mismo `bench_vel.py` que H-062, ahora con 3 pasadas y los casos `codigo-512`, `prosa-512` y `prefill-32k` (o `prefill-10k` en DeepSeek). Se da la mediana de 3 de la escritura (t/s).

## 1. `gufo bench` (las condiciones de la tabla oficial: pp2048, tg128, 3 repeticiones)

| Modo | pp2048 | tg128 |
| --- | ---: | ---: |
| AR (sin especulación) | 1.882 ± 11 | 30,2 ± 0,1 |
| MTP | 1.828 ± 1 | 62,6 ± 0,4 |

Gufo publica 1.735 de lectura y "hasta 117" de escritura con MTP. La lectura la superamos; los 117 no se reproducen con su propio banco en esta máquina (62,6). La cifra de "hasta" es un máximo con texto muy predecible; la escritura sin MTP (30) es el límite del ancho de banda.

## 2. Variantes del servidor de Qwen (mediana de 3, t/s de escritura)

| Variante | código | prosa | tras 32k |
| --- | ---: | ---: | ---: |
| producción (`--sessions 2`, borrador 1-7) | 68,6 | 38,6 | 41,1 |
| `--sessions 1` | 71,7 | 38,4 | 42,3 |
| `--draft-tokens 4` | 68,5 | 39,5 | 39,7 |
| `--min-draft-tokens 3` | 67,0 | 40,9 | 39,6 |

Todas las diferencias caen dentro de la dispersión entre pasadas (±3-4 t/s). Ninguna variante mejora la producción; `--sessions 1` no compensa perder la segunda sesión. La lectura de prompt no cambia (~1.570-1.600 t/s).

## 3. DeepSeek-V4.1-Flash: Gufo IQ1_M frente a Strata v0.2.13.1

| Motor | código | prosa | tras 10k | lectura 10k |
| --- | ---: | ---: | ---: | ---: |
| Gufo (feat/ds41) | 24,4 | 24,4 | 23,9 | ~1.015 t/s |
| Strata v0.2.13.1 | 26,4 | 19,3 | 21,5 | ~287 t/s |

Los 31,1 t/s del cron de actualizaciones (una sola petición, 200 tokens) no se reproducen. Strata solo gana en código corto (+2 t/s, en el límite del ruido) y pierde en prosa, en contexto y, sobre todo, en lectura de prompt (3,5×).

## Conclusión

No hay margen de configuración en Gufo con el hardware actual: la producción ya está en su punto. DeepSeek se queda en Gufo. El cron de actualizaciones pasa a mediana de 3 pasadas (umbral 0,92 + segunda tanda antes de rechazar), porque una sola medida le hizo anunciar 68,8 y 31,1 t/s que no se sostienen.
