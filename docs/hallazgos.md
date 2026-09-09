# Bitácora de hallazgos

Orden cronológico inverso. Incluye las conclusiones que resultaron ser **falsas**: dejarlas escritas evita repetir el error.

---

## H-008 · Con modelos razonadores, un `max_tokens` corto parece una alucinación
**Estado:** confirmado

Probando GLM-5.3-Flash con `max_tokens` de 32–160, el campo `content` volvía **vacío** en 4 de 5 pruebas. Parecía que el modelo no sabía responder. En realidad agotaba todo el presupuesto dentro del bloque de razonamiento (`reasoning_content`) y nunca llegaba a emitir la respuesta.

Ese modelo gasta entre 200 y 2.800 caracteres razonando antes de contestar; para "explica en dos frases" consumió 705 tokens y 87 segundos.

**Regla:** con modelos razonadores, dar `max_tokens` holgado (600–900 como mínimo) y **mirar siempre `reasoning_content` antes de concluir que el modelo falla**. Una respuesta vacía suele ser truncamiento, no incompetencia.

---

## H-007 · Lo que manda son los parámetros activos, no el tamaño del modelo
**Estado:** confirmado · **extiende H-006**

| Modelo | Totales | Activos | Tamaño | pp t/s | tg t/s |
|---|---|---|---|---|---|
| Qwen3.8-Flash-Next | 177B | ~3B | 87 GiB | 299,8 | **27,4** |
| GLM-5.3-Flash IQ1_S | 250B | ~18B | 93 GB | 124,8 | **8,3** |

Dos modelos que ocupan **lo mismo** en memoria y difieren **3,3×** en velocidad de generación. El de 250B es más grande en todo salvo en lo que importa aquí: activa seis veces más parámetros por token, y en una máquina limitada por ancho de banda eso se paga linealmente.

H-006 decía "MoE grande > denso mediano". Este hallazgo lo precisa: **no es el tamaño, es cuántos parámetros hay que leer por token**. Un MoE con muchos expertos activos se comporta como un denso grande.

**Corolario sobre cuantización:** el IQ1_S (≈1 bit por peso) **no rompió el modelo** — acertó las 5 pruebas de coherencia, incluidas aritmética, código y razonamiento temporal. La cuantización extrema resultó ser mucho menos problemática que los parámetros activos. Ver [`../benchmarks/glm53-flash.md`](../benchmarks/glm53-flash.md).

**Salvedad honesta:** el soporte de esa arquitectura no está mergeado en `llama.cpp`, Vulkan desactiva sus operaciones fusionadas y se ignora una capa entera (`nextn`). Las cifras son un **suelo**, no la última palabra.

---

## H-006 · El modelo denso mediano no tiene hueco en esta máquina
**Estado:** confirmado

| Modelo | Params activos | pp t/s | tg t/s |
|---|---|---|---|
| Qwen3.8-Flash-Next (177B MoE) | ~3B | 225,3* | 25,4* |
| Qwen3.8-27B denso | 27B | 365,1 | 13,1 |

\* Cifras de la primera caracterización, antes de optimizar `ubatch` (luego 299,8 / 27,4).

El MoE de 177B **genera al doble** que el denso de 27B pese a ocupar cinco veces más. Con memoria unificada el limitante es el ancho de banda: por cada token hay que leer los pesos *activos*, y el MoE activa ~3B frente a 27B del denso.

**Regla:** en Strix Halo, elegir por parámetros activos, no por tamaño total. El denso mediano es el peor de los dos mundos — ni cabe entero en caché ni activa poco.

---

## H-005 · `llama-bench` con otro modelo da recomendaciones opuestas
**Estado:** confirmado · **corrige a H-004**

Barrido de `ubatch` con un modelo pequeño → curva **descendente** (997 → 853 → 680 t/s), recomendación `ub 1024`.
Barrido contra `llama-server` con el modelo real y prompt real de 32.951 tokens → curva **ascendente**, ganador `ub 4096`.

| ubatch | pp t/s | tg t/s |
|---|---|---|
| 512 | 259,6 | 27,36 |
| 1024 | 287,4 | 25,31 |
| 2048 | 296,7 | 27,25 |
| **4096** | **299,8** | **27,35** |

3 pasadas por punto, reinicio del servicio entre configuraciones.

**Regla:** no extrapolar parámetros de batch entre modelos. Medir con el modelo que vas a servir, con la carga que vas a servir.

---

## H-004 · ~~`ub 1024` es el óptimo~~ — **ERRÓNEO**
**Estado:** ❌ refutado por H-005. Conclusión sacada de `llama-bench` con un modelo distinto al de producción.

---

## H-003 · El carveout UMA de la BIOS es irrelevante con RADV
**Estado:** confirmado

Misma carga, antes y después de ampliar el *UMA Frame Buffer*:

| Carveout | MemTotal | VRAM | GTT | pp t/s | tg t/s |
|---|---|---|---|---|---|
| mínimo | 62 GB | — | — | 1.279,2 | 45,34 |
| ampliado | 124,5 GB | 1 GB | 124 GB | 1.286,8 | 45,42 |

Diferencia dentro del ruido. RADV expone VRAM+GTT como un único pool (`uma:1`), así que el reparto en BIOS no cambia lo que puede usar la inferencia.

**Regla:** dejar el carveout al mínimo y controlar la memoria por `amdgpu.gttsize` + `ttm.pages_limit`.

---

## H-002 · SELinux bloquea binarios fuera de `/usr` como servicio
**Estado:** confirmado

`llama-server` compilado en un directorio propio arranca a mano, pero como unidad systemd falla con `203/EXEC — Permission denied`. No es permiso de fichero: es la etiqueta SELinux (Fedora en *Enforcing*).

Solución: etiquetar el directorio de binarios como `bin_t` con `semanage fcontext` + `restorecon`. Ver [`servicio-systemd.md`](servicio-systemd.md).

---

## H-001 · Una ventana de 256k es viable aquí
**Estado:** confirmado

El modelo en producción declara `context_length = 262144` nativo (RoPE base 10M) y se sirve entero. Con caché KV unificada, dos slots simultáneos ven **cada uno** los 262.144 tokens en lugar de repartirlos.

Es sostenible porque solo 12 de las 48 capas son de atención (2 cabezas KV + indexador disperso); las otras 36 son lineales de estado fijo, cuyo coste **no crece** con la longitud. Ver [`ventana-de-contexto.md`](ventana-de-contexto.md).

**Pendiente:** se observó un cuelgue de GPU alrededor de 90k tokens con `ubatch` alto. Sin reverificar.
