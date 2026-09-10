# Bitácora de hallazgos

Orden cronológico inverso. Incluye las conclusiones que resultaron ser **falsas**: dejarlas escritas evita repetir el error.

---

## H-012 · Contexto largo: la generación cae a la mitad a 98k, pero la aguja se recupera siempre
**Estado:** confirmado · 6 puntos × 2 pasadas contra `llama-server` + prueba de aguja por punto

Barrido de longitud de contexto con la configuración de producción (`ubatch 2048`,
`lazy off`), sin reiniciar el servicio: la única variable es el tamaño del prompt.
En cada punto, además del rendimiento, se enterró un código único **a la mitad** del
texto y se le pidió al modelo recuperarlo (los extremos son la parte fácil: primacía
y recencia).

| prompt real (tokens) | pp (t/s) | tg (t/s) | latencia total | aguja |
|---:|---:|---:|---:|:---:|
| 3.065 | 308,8 | 26,32 | 11,0 s | OK |
| 12.065 | 365,4 | 24,56 | 34,7 s | OK |
| 24.041 | 347,1 | 22,75 | 70,9 s | OK |
| 48.761 | 284,3 | 17,38 | 173,1 s | OK |
| 74.993 | 240,1 | 14,50 | 314,5 s | OK |
| 98.201 | 209,3 | 13,03 | 470,2 s | OK |

**Lecturas:**

1. **La generación cae a la mitad** (26,3 → 13,0 t/s, −50%) entre 3k y 98k. Es el KV
   cache: por cada token generado hay que releer un KV que crece linealmente con el
   contexto, y en una máquina limitada por ancho de banda eso se paga entero. El
   prefill aguanta mejor (−32%).
2. **El máximo de prefill no está en el prompt más corto** (365 t/s a 12k > 309 a 3k):
   con 3k tokens no hay trabajo suficiente para amortizar el arranque de los kernels.
   Los números de marketing con prompts diminutos subestiman el prefill real.
3. **Recuperación perfecta 6/6** con el dato enterrado a la mitad del texto, hasta
   98k tokens. La ventana anunciada de 262k no se verificó entera, pero hasta 98k es
   ventana *útil*, no solo reservada.
4. **Repetibilidad excelente**: las dos pasadas de cada punto difieren <1%
   (p. ej. 208,8 vs 209,9 a 98k).
5. **Coste práctico**: meter ~100k tokens cuesta **~8 minutos** de prefill. Para RAG
   con documentos largos, esto manda más que el tg.

**Nota de método:** el estimador tokens/palabra (1,78, calibrado con `prompt_n` real
a 33k) se queda corto en prompts cortos (pedí 4k, salieron 3.065; −23%): la
tokenización no es lineal con la repetición. Por eso la tabla registra `prompt_n`
real y no el objetivo. Servicio estable todo el barrido: 0 reinicios.

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

## H-013 — Qwen3-Next-80B-A3B: generación casi plana hasta 122k (2026-09-09)

Barrido de contexto idéntico al de H-012, mismo método (2 pasadas por punto, aguja
de código enterrada a mitad de texto, `prompt_n` real medido), sobre
**Qwen3-Next-80B-A3B-Instruct IQ4_XS** (42,6 GB, arquitectura `qwen3next`: MoE con
atención híbrida lineal/gated). Servidor en :8081, `-ub 2048`, `-lzm off`, `-c 262144`.

| prompt_n | pp t/s | tg t/s | aguja |
|---:|---:|---:|:---|
| 3.772 | 792,5 | 45,3* | OK |
| 15.022 | 779,0 | 33,5 | OK |
| 29.992 | 680,0 | 31,7 | OK |
| 60.892 | 487,3 | 28,7 | OK |
| 93.682 | 321,8 | 25,8 | OK |
| 122.692 | 239,7 | 24,6 | OK |

\* pasada caliente; la fría dio 9,75 t/s (primer toque de expertos) y se descarta.

**Lo importante no es que sea rápido: es CÓMO decae.** Del punto de 15k al de 122k
la generación solo pierde un 27% (33,5 → 24,6), y de 61k a 122k apenas un 14%.
El Flash-Next en el mismo barrido caía de 24,6 (12k) a 13,0 (98k), un −47%.
La causa es arquitectural: `qwen3next` usa atención híbrida (la mayoría de capas
son lineales, solo unas pocas hacen atención completa), así que el coste por token
crece mucho más despacio con el contexto que en un transformer clásico.

Comparativa directa en los puntos comunes (Flash-Next 87 GiB vs 80B 42,6 GiB):

| contexto ≈ | Flash-Next pp/tg | 80B pp/tg | ventaja 80B |
|---:|---:|---:|:---|
| 12–15k | 365 / 24,6 | 779 / 33,5 | 2,1x / 1,4x |
| 24–30k | 347 / 22,8 | 680 / 31,7 | 2,0x / 1,4x |
| 49–61k | 284 / 17,4 | 487 / 28,7 | 1,7x / 1,7x |
| 94–98k | 209 / 13,0 | 322 / 25,8 | 1,5x / 2,0x |

Y a 122k el 80B (240 / 24,6) sigue siendo más rápido que el Flash-Next a 12k en
generación. Todo con **la mitad de memoria** (54 GB en uso frente a 109) y 6/6
agujas correctas: la ventana larga no es decorativa.

**Consecuencia práctica:** el 80B es el candidato natural a modelo de producción
del M5. Libera ~55 GB, suficiente para convivir con un pipeline de difusión de
imagen (fase B del plan). Queda pendiente la batería de calidad (A8) antes de
promocionarlo: velocidad no es inteligencia.

**Método:** los objetivos pedidos (4k…131k) produjeron `prompt_n` reales un 6-8%
menores; confirmado el sesgo del estimador de H-012. Los 131k reales del límite
publicitado siguen sin tocarse: harían falta ~140k pedidos.

## H-014 — Primer crash de GPU del laboratorio: watchdog del kernel vs prefill largo (2026-09-10)

Al pedir ~180k tokens al 80B con `-ub 2048`, el prefill murió a ~168k con
`vk::Queue::submit: ErrorDeviceLost`. En `dmesg`:

```
amdgpu: ring comp_1.2.0 timeout, signaled seq=1379627, emitted seq=1379629
amdgpu: Process llama-server pid 9675
amdgpu: Ring comp_1.2.0 reset succeeded
amdgpu: [drm] device wedged, but no recovery needed
```

**Diagnóstico:** a esa profundidad de contexto, un dispatch de `ub=2048` tarda más
que el watchdog de amdgpu; el kernel declara la cola colgada y resetea el ring
(el sistema se recupera solo, sin reinicio — bien por Fedora 44 + kernel 7.1).

**Solución verificada:** relanzar con `-ub 512` → dispatches ~4x más cortos.
Resultado a 168.562 tokens reales: **314,9 pp / 20,9 tg, aguja OK, 0 timeouts nuevos**.
Curiosamente el prefill medio con ub=512 (314,9) supera al de ub=2048 a 122k (239,7):
a contextos extremos los dispatches cortos también rinden más.

**Reglas operativas nuevas:**
1. `-np` divide el contexto entre slots: con `-np 2` y `-c 262144` cada petición
   tope es ~131k y el servidor devuelve **HTTP 400** al pasarse. Para contexto
   máximo en una petición: `-np 1`.
2. Por encima de ~131k de prompt, usar `-ub 512` (o menor). El crash es
   reproducible con ub=2048.
3. El punto de 131.122 tokens (np=1, ub=2048): 226,3 pp / 25,0 tg, aguja OK.

**Curva completa del 80B (7 puntos, 8/8 agujas):** generación 45→21 t/s de 4k a
168k; a 168k tokens este MoE de 80B sigue generando un 60% más rápido que el
Flash-Next a 98k (13,0 t/s).
