# Qwen3.8-Flash-Next — caracterización completa

Modelo en producción en esta máquina.

## Ficha

| | |
|---|---|
| Arquitectura | MoE híbrido, 48 capas (12 de atención + 36 lineales) |
| Parámetros | ~177B totales · ~3B activos por token |
| Cuantización | UD-IQ4_XS (dinámica) |
| Tamaño en memoria | ~87 GiB |
| Ventana nativa | 262.144 tokens (RoPE base 10M) |
| Multimodal | Sí (proyector de visión cargado) |

## Barrido de ubatch

**Condiciones:** prompt real de **32.951 tokens**, 3 pasadas por punto, **reinicio del servicio entre puntos**, medido leyendo `timings` de la respuesta de `llama-server`.

| batch / ubatch | pp (t/s) | tg (t/s) |
|---|---|---|
| 512 | 259,6 | 27,36 |
| 1.024 | 287,4 | 25,31 |
| 2.048 | 296,7 | 27,25 |
| **4.096** | **299,8** | **27,35** |

### Lectura

- El **prefill gana un 15%** de 512 a 4096. Es cómputo: lotes mayores aprovechan mejor las unidades de la iGPU.
- La **generación es plana** (~27 t/s en todos los puntos). Ahí el cuello es el ancho de banda de memoria, y `ubatch` no lo toca. El 25,31 de la fila de 1024 es una anomalía de esa tanda, no una tendencia.
- Sin ganancia adicional esperable por encima de 4096, y el riesgo de inestabilidad a contexto largo sube.

**Configuración adoptada:** `--batch-size 4096 --ubatch-size 4096`.

## Aviso importante

Un barrido previo hecho con `llama-bench` **y un modelo distinto** dio la curva **al revés** y recomendaba `ub 1024`. Fue un error. Ver [`../docs/hallazgos.md`](../docs/hallazgos.md) H-004/H-005.

## Pendiente

Todas estas cifras son a **33k tokens**. A ~90k con `ubatch` 4096 se observó un cuelgue de GPU, sin reverificar. Falta el barrido a contexto largo.
