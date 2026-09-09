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

## Barrido de ubatch — vigente (`--lazy-mode off`)

**Condiciones:** prompt real de **24.782 tokens**, 2 pasadas medidas + 1 descartada por
punto, **servicio parado y rearrancado entre puntos**, `timings` leídos de la respuesta de
`llama-server`. Build `9113cc1`.

| batch / ubatch | pp (t/s) | tg (t/s) | vs 512 |
|---|---:|---:|---:|
| 512 | 307,9 | 22,16 | — |
| 1.024 | 335,7 | 22,16 | +9,0% |
| **2.048** | **345,0** | **22,24** | **+12,0%** |
| 4.096 | **no arranca** | — | OOM al cargar |

### Lectura

- El **prefill gana un 12%** de 512 a 2048, y la curva **se corta ahí**: `ubatch 4096` ya
  no arranca (muere por OOM durante la carga, systemd reintenta en bucle). Sin carga
  diferida los pesos no son reclamables y los buffers de 4096 no caben en 124 GB.
- El salto de 1024 a 2048 es de solo **+2,8%**. Si necesitas margen de memoria, 1024 es una
  concesión baratísima.
- La **generación es plana** (~22,2 t/s en los tres puntos): el cuello es el ancho de banda
  de memoria y `ubatch` no lo toca.

**Configuración adoptada:** `--batch-size 4096 --ubatch-size 2048`.

> `tg` = 22,2 t/s aquí frente a 27,4 t/s en pruebas de contexto corto. No es regresión:
> generar después de un prefill de 24k obliga a recorrer un KV mayor por token. Compara
> `tg` solo entre medidas hechas a la misma longitud de contexto.

## Barrido anterior (histórico, con carga diferida activa)

Medido igual pero con el valor por defecto `--lazy-mode auto`, y con un prompt de 32.951
tokens (el estimador de tokens del script estaba mal calibrado; ver más abajo).

| batch / ubatch | pp (t/s) | tg (t/s) |
|---|---:|---:|
| 512 | 259,6 | 27,36 |
| 1.024 | 287,4 | 25,31 |
| 2.048 | 296,7 | 27,25 |
| 4.096 | 299,8 | 27,35 |

Se conserva porque cuantifica lo que aporta `lazy off` a igual `ubatch`: **+18,6%** (512),
**+16,8%** (1024), **+16,3%** (2048). Nótese que es bastante menos que el **+92%** que dio
`llama-bench`: el banco sintético exagera la mejora.

**Error corregido:** en su día adopté `ubatch 4096` a partir de esta tabla. Era un +1% sobre
2048 y hoy directamente no arranca. Ver [`../docs/hallazgos.md`](../docs/hallazgos.md) H-011.

## Aviso importante

Un barrido previo hecho con `llama-bench` **y un modelo distinto** dio la curva **al revés** y recomendaba `ub 1024`. Fue un error. Ver [`../docs/hallazgos.md`](../docs/hallazgos.md) H-004/H-005.

Un segundo barrido con `llama-bench` (esta vez con el modelo correcto y `lazy off`) volvió a
discrepar del servidor real: daba el máximo en **1024** (336,2 t/s) y **caída** en 2048
(319,5) y 4096 (257,3). El servidor real dice **2048**. Se mantiene la regla del laboratorio:
**decide `llama-server`, no `llama-bench`.**

## Barrido de longitud de contexto (`ubatch 2048`, `lazy off`)

**Condiciones:** configuración de producción sin tocar (servicio **no** reiniciado entre
puntos: la única variable es el prompt), 2 pasadas por punto, `timings` de `llama-server`,
y en cada punto una **prueba de aguja**: un código único enterrado a la mitad del texto
que el modelo debe recuperar. Se registra `prompt_n` real, no el objetivo del script.

| prompt real (tokens) | pp (t/s) | tg (t/s) | latencia total | aguja |
|---:|---:|---:|---:|:---:|
| 3.065 | 308,8 | 26,32 | 11,0 s | OK |
| 12.065 | 365,4 | 24,56 | 34,7 s | OK |
| 24.041 | 347,1 | 22,75 | 70,9 s | OK |
| 48.761 | 284,3 | 17,38 | 173,1 s | OK |
| 74.993 | 240,1 | 14,50 | 314,5 s | OK |
| 98.201 | 209,3 | 13,03 | 470,2 s | OK |

### Lectura

- **La generación cae a la mitad** entre 3k y 98k (−50%): el KV cache se relee entero por
  token generado y crece con el contexto. El prefill aguanta mejor (−32%).
- **El máximo de prefill está en ~12k, no en el prompt más corto**: a 3k no hay trabajo
  suficiente para amortizar el arranque de los kernels.
- **Aguja 6/6 hasta 98k** con el dato a la mitad del texto: ventana útil verificada, no
  solo reservada.
- **Repetibilidad <1%** entre pasadas. Servicio estable: 0 reinicios en ~70 min de barrido.
- Coste práctico: **~100k tokens ≈ 8 min de prefill**.

Detalle y método: [`../docs/hallazgos.md`](../docs/hallazgos.md) H-012 ·
script [`../scripts/bench-context.py`](../scripts/bench-context.py).

## Pendiente

Verificar la ventana completa de **262.144** de punta a punta (el barrido llegó a 98k por
la calibración del estimador de tokens; el siguiente paso es 131k reales y 262k).

---

## Nota posterior: el barrido histórico está infravalorado

El barrido histórico se midió con la **carga diferida de tensores activa** (el valor por
defecto de la build `9113cc1`). Con `--lazy-mode off` el prefill sube **+16-19%** contra el
servidor real a igual `ubatch` (el +92% que dio `llama-bench` era el banco sintético
exagerando). Sigue siendo válido *como comparación relativa entre valores de `ubatch`*,
pero sus valores absolutos de `pp` son un suelo.

👉 [`../docs/carga-diferida-y-oom.md`](../docs/carga-diferida-y-oom.md)
