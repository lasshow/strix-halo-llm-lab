# Bitácora de hallazgos

Orden cronológico inverso. Incluye las conclusiones que resultaron ser **falsas**: dejarlas escritas evita repetir el error.

---

## H-011 · Con la carga diferida desactivada, `ubatch 4096` ya no arranca
**Estado:** confirmado · barrido sobre `llama-server`, 2 pasadas medidas por punto + 1 descartada

Rehecho el barrido de `ubatch` con `--lazy-mode off` (H-009), porque el publicado se midió
con la carga diferida activa y el óptimo podía haberse movido. Se movió.

**Prompt real de 24.782 tokens, servicio reiniciado entre puntos:**

| ubatch | pp (t/s) | tg (t/s) | vs 512 |
|---:|---:|---:|---:|
| 512 | 307,9 | 22,16 | — |
| 1.024 | 335,7 | 22,16 | +9,0% |
| **2.048** | **345,0** | **22,24** | **+12,0%** |
| 4.096 | **no arranca** | — | OOM al cargar |

`ubatch 4096` es directamente **inviable**: el servidor muere por OOM durante la carga y
systemd entra en bucle de reintentos (llegó a 20). Antes sí arrancaba porque con la carga
diferida los pesos eran reclamables; sin ella, los buffers de un ubatch de 4096 ya no caben.

**Esto cierra la corrección de H-006.** Publiqué `ubatch 4096` como configuración
recomendada. No solo la ventaja era de ~1%: en la configuración actual **ni siquiera
arranca**. La recomendación correcta es **2048**, y el margen sobre 1024 es de solo 2,8%.

**Ganancia de `lazy off` a igual `ubatch`** (comparando con el barrido anterior, mismo
script y mismo prompt): +18,6% / +16,8% / +16,3% para 512 / 1024 / 2048. Consistente, y
más modesta que el +92% que midió `llama-bench` — otra razón para medir contra el servidor
real y no contra el banco sintético.

**Efecto colateral valioso:** este OOM ocurrió con `OOMScoreAdjust=+500` ya aplicado
(H-010) y la máquina **siguió accesible por SSH** todo el tiempo. El mismo fallo que ayer
costó un reinicio físico hoy solo costó un servicio caído. El arreglo está validado en un
incidente real, no en teoría.

**Nota sobre `tg`:** 22,2 t/s aquí frente a 27,4 t/s en las medidas cortas. No es una
regresión: generar tras un prefill de 24k tokens obliga a recorrer un KV cache mucho mayor
en cada token. Comparar solo cifras de `tg` tomadas a la misma longitud de contexto.

---

## H-010 · `OOMScoreAdjust` negativo convierte un OOM en una caída total
**Estado:** confirmado · **coste:** un reinicio físico

Al pasar el servicio a `--lazy-mode off` (H-009), la máquina quedó **inalcanzable**:
respondía al ping pero SSH rechazaba la conexión, y no se recuperó sola en 15 minutos.

La unidad systemd llevaba `OOMScoreAdjust=-500`, que le dice al kernel *«mata cualquier
cosa antes que a este proceso»*. Cuando faltó memoria, el kernel obedeció literalmente y
fue matando `sshd`, `NetworkManager`, `systemd-resolved`, `tailscaled`, `polkit`, `crond`
y `auditd` — todo para salvar al servidor de inferencia, que **murió igualmente** después.

```
Out of memory: Killed process 1871 (llama-server) ... oom_score_adj:-500
```

**Por qué faltó memoria:** con `lazy auto` los pesos van mapeados a fichero y el kernel
puede reclamarlos; con `lazy off` los 87 GiB quedan residentes y **no reclamables**. Con
el KV de 262.144 y 24 GB de `--cache-ram` encima, no cabía en 124 GB.

**Corrección:** `OOMScoreAdjust=500` (positivo), `--cache-ram` 24576 → 4096,
`--ubatch-size` 4096 → 2048. Verificado: arranca en 30 s, `oom_score_adj = 0`,
0 reinicios, 27,2–27,4 t/s en peticiones consecutivas, presión de memoria 0.

**Regla:** un servicio que ocupa el 80% de la RAM debe ser lo **primero** en morir, nunca
lo último. Si muere el servicio, systemd lo reinicia; si muere la máquina, hay que ir a
pulsar el botón. Nunca `OOMScoreAdjust` negativo en un servidor de inferencia.

**Diagnóstico falso que descarté por el camino:** culpé a `brush-server` de competir por
memoria. El log lo desmiente: consumió 10,2 MB de pico. No tuvo nada que ver.

👉 [`carga-diferida-y-oom.md`](carga-diferida-y-oom.md)

---

## H-009 · La carga diferida de tensores cuesta la mitad del prefill en iGPU
**Estado:** confirmado · A/B con `-r 3`

| `lazy_mode` | pp512 | tg128 |
|---|---:|---:|
| `auto` (por defecto) | 216,03 ± 0,81 | 25,45 ± 0,25 |
| **`off`** | **415,38 ± 4,43** | **27,68 ± 0,02** |
| | **+92%** | +8,8% |

El prefill casi se dobla con una sola flag y **sin recompilar**: el binario ya soportaba
`-lzm`. El fix que desactiva la carga diferida en iGPUs (`f3f1a8f`, PR #28326) entró
**diez horas después** del commit de la build de producción (`9113cc1`).

Que la generación apenas mejore es coherente: `tg` está limitada por ancho de banda de
memoria, no por cómo se carguen los tensores.

**Cómo se detectó, y es lo reutilizable:** comparando el rendimiento medido con el techo
teórico de ancho de banda. Los modelos densos rendían al **78–87%** de su techo; el MoE se
quedaba en el **19%**. Un modelo que va desproporcionadamente mal *respecto a sí mismo*
apunta a un problema de software, no de hardware.

**Consecuencia:** todas las cifras de prefill anteriores del cuaderno están
infravaloradas. El CSV incorpora ya columnas `commit` y `lazy_mode`.

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
| GLM-5.3-Flash IQ1_S | 313B | ~18B | 93 GB | 124,8 | **8,3** |

Dos modelos que ocupan **lo mismo** en memoria y difieren **3,3×** en velocidad de generación. El de 313B es más grande en todo salvo en lo que importa aquí: activa seis veces más parámetros por token, y en una máquina limitada por ancho de banda eso se paga linealmente.

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
