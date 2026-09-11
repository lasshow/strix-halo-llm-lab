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

> ⚠️ **TABLA CONTAMINADA — corregida por [H-022](#h-022--con-batch-fijo-la-ventaja-de-ubatch-2048-sobre-1024-desaparece-h-011-estaba-contaminado-2026-09-10).**
> El script usado movía `--batch-size` **junto con** `--ubatch-size`, así que cada fila es
> una configuración distinta (batch 512+ubatch 512, batch 1024+ubatch 1024…) y las filas
> **no son comparables entre sí**. El `+12,0%` no es el efecto de `ubatch`. Con `batch`
> fijo a 4096, 1024 y 2048 rinden igual dentro del ruido. Se conserva por método.

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

## H-015 — Actualizar llama.cpp casi duplica la generación del 80B (2026-09-10)

Build `9113cc1` (8-sep) → `72797e8` (10-sep, 22 commits). Dos cambios relevantes
para este hardware: shader mat-vec **dedicado a IQ4_XS** en Vulkan (`df750f7`) y
lazy-loading desactivado por defecto en iGPUs (`f3f1a8f`, #28326).

`llama-bench` pp512/tg128, mismos parámetros que la build vieja:

| Modelo | pp512 (vieja→nueva) | tg128 (vieja→nueva) |
|---|---|---|
| Qwen3-Next-80B IQ4_XS | 738 ≈ 780 | **33,5 → 62,6 (+87%)** |
| Flash-Next UD-IQ4_XS | **225 → 416 (+85%)** | 25,4 → 27,3 (+8%) |
| Qwen3-8B Q4_K_M (control) | 1287 → 1273 | 45,4 → 43,7 |

- El control Q4_K_M no se mueve → la mejora es del shader IQ4_XS, no genérica.
- El +85% de prefill del Flash-Next confirma H-011/#28326: nuestra build de
  producción cargaba con lazy-loading activo.
- El 80B ahora genera **62,6 t/s** en corto: 2,3x el Flash-Next nuevo y más que
  el 8B denso de la build vieja. Un MoE de 80B generando más rápido que un denso
  de 8B de hace dos días.

**Lección operativa:** en este ecosistema, *no actualizar* llama.cpp durante una
semana puede costar 2x de rendimiento. Re-medir tras cada actualización pasa a
ser parte del protocolo (columna `commit` del CSV ya lo soporta).

**Pendiente derivado:** re-barrer contexto largo del 80B con la build nueva
(los 226 pp / 25 tg a 131k de H-014 son de la build vieja) y re-decidir la
promoción con la batería de calidad A8.

## H-016 — Batería de calidad A8: la velocidad no promociona sola (2026-09-10)

20 prompts fijos, temperatura 0, mismos para ambos modelos (razonamiento, código
Python/Kotlin/SQL/bash, JSON estricto, castellano/euskera, seguimiento de
instrucciones, conocimiento industrial). Corrección manual + ejecución real del
código generado. La batería contiene datos de contexto privado y no se publica;
las categorías y la corrección sí.

| Categoría (n) | Flash-Next 177B | Qwen3-Next-80B |
|---|---|---|
| Razonamiento/lógica (4) | 4 | 3,5 |
| Código (4) | 3 | **4** |
| JSON estricto (3) | 3 | 3 |
| Castellano/euskera (4) | **4** | 2 |
| Instrucciones exactas (3) | 2,5 | 1 |
| Conocimiento industrial (2) | 2 | 2 |
| **Total /20** | **18,5** | **15,5** |

Detalle de las diferencias:

- **Código:** el 80B escribió un `parse_iso_duration()` que pasa los 4 casos de
  prueba ejecutados de verdad; el Flash-Next quemó **8.192 tokens de
  razonamiento sin emitir respuesta** (0 directo). Con el presupuesto de
  producción (2k) el Flash-Next dejó **3 de 20 prompts sin responder** por
  gastarlo entero en pensar; hubo que repetirlos con 8k.
- **Idioma:** el 80B falló la corrección ortográfica castellana («Habrá si
  mañana…», dejó un «sino» sin corregir) y su euskera es inventado
  («fornoa gauza egongo da maintenantsa»); el Flash-Next corrigió perfecto y
  su batua es correcto.
- **Instrucciones exactas:** el 80B afirma «(12 palabras)» en una frase de 16;
  el Flash-Next cumplió el conteo exacto. Ninguno clavó el ejercicio de líneas
  con n palabras (3/5 vs 2/5 líneas correctas).
- **Velocidad total de la batería:** 80B **81 s** vs Flash-Next **613 s + reintentos**
  (~7,5x más lento: piensa antes de cada respuesta).

**Veredicto de promoción:** el Flash-Next **sigue en producción**. El 80B es
2,3x más rápido generando (H-015) y mejor en código, pero pierde en idioma e
instrucciones — y producción se usa en castellano/euskera a diario. Además el
Flash-Next aporta visión (mmproj). Rol del 80B: **motor rápido secundario**
para tareas de código/latencia baja, arrancable bajo demanda en :8081
(`/root/start80b.sh`).

**Matiz operativo importante:** con `max_tokens` 2k, un modelo razonador puede
devolver la respuesta VACÍA (todo el presupuesto se va en `reasoning_content`).
Para clientes del Flash-Next: presupuestar ≥4k o limitar el razonamiento.

## H-017 — Aguja a 124k en el Flash-Next: 3/3, pero el precio es el prefill (2026-09-10)

**Qué se midió.** Prueba de aguja en el modelo de producción
(Qwen3.8-Flash-Next UD-IQ4_XS, servicio `llama-flashnext`, commit `311d421`),
con un prompt real de **124.196 tokens** de relleno en castellano (informes de
mantenimiento industrial) y la aguja insertada al **10%, 50% y 90%** de la
ventana. Pregunta: recuperar un código de calibración literal.

**Resultado: 3 de 3 recuperadas**, respuesta exacta y sin adornos en las tres
profundidades (`AZUL-4417-KAPINET`). No hay zona muerta a media ventana ni
sesgo hacia el final del prompt en este tamaño.

| profundidad | prompt tokens | pp (tok/s) | tg (tok/s) | total |
|---|---|---|---|---|
| 0.1 | 124.196 | 144,4 | 11,68 | 870 s |
| 0.5 | 124.196 | 147,7 | 11,61 | 852 s |
| 0.9 | 124.154 | 147,4 | 11,62 | 853 s |

**Lo que de verdad importa aquí no es la aguja, es el coste.** Comparado con
el punto corto de H-015 (pp 416 / tg 27,3 a 512 tokens):

- El **prefill cae a ~1/3** (416 → 145 tok/s) y, al ser lineal en el número de
  tokens, un prompt de 124k tarda **~14 minutos solo en leerse**.
- La **generación cae a ~43%** (27,3 → 11,6 tok/s): la atención sobre 124k de
  KV pesa más que los 3B de parámetros activos.
- El tiempo de respuesta está dominado al **99%** por el prefill (842 s de 852 s).

**Consecuencia operativa:** la ventana de 262k del servicio es real y utilizable,
pero un prompt de seis cifras de tokens **no es interactivo** en esta máquina.
Para uso diario conviene mantener `--cache-reuse` haciendo su trabajo (prompts
que crecen sobre un prefijo ya procesado) y evitar reenviar contextos enormes
desde cero. Un RAG con 8-16k de contexto recuperado es el patrón sensato; volcar
el documento entero no lo es.

**Nota de método (fallo propio, corregido).** El primer script estimó «~16
tokens por bloque» de relleno y generó sin querer un prompt de ~186k tokens,
por encima del límite por slot (`-c 262144` con `-np 2` = **131.072 tokens
efectivos por slot**). Se abortó y se reescribió calibrando contra el endpoint
`/tokenize` del propio servidor: **47,91 tokens por bloque**. Regla para el
laboratorio: el tamaño de un prompt sintético se **mide con el tokenizador del
modelo**, nunca se estima.


## H-018 — GLM-5.3-Flash-REAP50 arranca en Strix Halo, pero no compensa (2026-09-10)

**Qué se probó.** El GGUF `patrickbdevaney/GLM-5.3-Flash-REAP50-GGUF` IQ4_XS (82 GiB, 321B podado al 50% de
expertos → ~165B) sobre el M5. El modelo usa hiperconexiones mHC y no lo carga ningún `llama.cpp` de upstream:
hace falta un build parcheado. Se compiló aparte, en `/models/llama.cpp-glm5`, desde la rama
`glm5-next-reap50-gguf-v1` (`2a4a412`), sin tocar el build productivo `/models/llama.cpp` (`311d421`).

**Trampas encontradas.**

1. `llama-server` **aborta al arrancar** con `GGML_ASSERT(obj_new) failed` en `ggml_reshape_2d`, dentro de
   `llama_params_fit` (el auto-ajuste de memoria de las builds nuevas). Se resuelve con **`-fit off`**.
   Sin esa bandera el modelo no llega ni a cargarse, con GPU o sin ella.
2. Los dos `--override-kv` de stop tokens que pide el README **deben ir separados por coma en un solo
   argumento**: repetir la bandera hace que llama.cpp descarte el primero (`DEPRECATED: argument
   '--override-kv' specified multiple times ... only last value will be used`).
3. Con el servicio de producción levantado (73 GB) no cabe: 82 + 73 > 124 GB y la máquina pagina desde disco
   hasta hacer inservible la prueba. Hay que **parar `llama-flashnext` antes** de medir.
4. `llama-cli -no-cnv` ya no existe en esta base: `--no-conversation is not supported by llama-cli, please
   use llama-completion instead`.

**Medidas propias** (prompt real de 2 273 tokens en castellano, `-c 8192 -np 1 -fa on --threads 16`):

| backend | pp (tok/s) | tg (tok/s) |
|---|---|---|
| CPU (`-ngl 0 --no-repack`, como recomienda el README del autor) | 63,22 | 6,32 |
| **Vulkan RADV (`-ngl 99`)** | **148,14** | **12,55** |

**Corrección al README del modelo:** la recomendación de `-ngl 0 --no-repack` para memoria unificada **no
aplica aquí**. En este equipo descargar todo a la iGPU no da OOM (Vulkan0 reporta 127 834 MiB libres y toma
83 639 MiB de tensores) y va **2,3× más rápido en prefill y 2× en generación**. El consejo del autor
probablemente venga de máquinas con carveout pequeño.

**Calidad.** Aritmética correcta (17×23 → 391, con razonamiento visible) y código Python válido. Pero:
*el modelo está roto para castellano y euskera*. Responde en inglés a preguntas en español, y cuando se le
fuerza el idioma produce mezclas — «El text is a repeated notice...», «raise ValueError("La list cannot be
empty")» — o directamente **devuelve contenido vacío** (2 de 3 prompts en castellano/euskera acabaron en
respuesta vacía gastando el presupuesto en razonamiento). Encaja con el coste del pruning que publica el
propio autor: los dominios no anglosajones y de «ballast» son los que más se degradan (agreement 0,580 en
ballast frente a 0,919 en código).

**Veredicto.** No entra en producción. Frente al Qwen3.8-Flash-Next que ya sirve el M5 (pp 404 / tg 27,4 a
contexto corto) es **2,7× más lento en prefill y 2,2× en generación**, ocupa lo mismo, exige un build propio
fuera de upstream y falla en el idioma de trabajo. Queda como curiosidad: es la primera vez que este
laboratorio corre una arquitectura con hiperconexiones mHC, y el build parcheado se conserva en
`/models/llama.cpp-glm5` por si la rama entra algún día en upstream.


## H-019 — Calidad de Qwen3.8-Flash-Next: bien en idioma y código, pero se cuelga razonando (2026-09-10)

**Qué se probó.** Batería propia de 27 prompts (`private/prompts.json` v2) contra el servicio productivo
`llama-flashnext`, `temperature 0`, `max_tokens 4096`, una pasada por prompt. Reparto: 6 de código con
lenguajes verificables en el A8 (Rust, TypeScript, JS/Node, Python, SQL), 4 de «código nuevo» como prueba de
alucinación de APIs recientes (Go 1.23 range-over-func, Zig, Swift 6, WGSL), 3 castellano, 2 inglés, 2 chino,
3 de seguimiento literal de instrucciones, 2 de JSON estricto, 2 de razonamiento, 1 de lógica, 2 de
conocimiento técnico. **Sin euskera** en esta tanda.

Regla de esta fase: **el código no se corrige a ojo**. Se extrae el bloque y se compila y ejecuta de verdad
(`scripts/verifica-codigo.py`): `rustc 1.98.1`, `tsc 6.0.3`, `node 22.23.1`, `python3 3.11`, `sqlite3 3.45`,
más `go 1.22.2` (con `GOEXPERIMENT=rangefunc`, porque los iteradores range-over-func no son estables hasta
1.23) y `zig 0.15.1` instalados para esta prueba, y WGSL compilado y **ejecutado en la iGPU del
propio A8** (Radeon 780M, RADV, vía `wgpu` 0.32).

**Rendimiento durante la batería** (27 prompts reales, no sintéticos): tg mediana **26,6 t/s**, pp mediana
**44,3 t/s**. El tg encaja con los 27,4 t/s de referencia; el pp bajo es esperable porque estos prompts son
de 30-120 tokens y el prefill no llega a amortizarse.

### El hallazgo importante: bucles de razonamiento que devuelven vacío

**5 de 27 prompts agotaron los 4 096 tokens sin emitir una sola palabra de respuesta**: todo el presupuesto
se fue en `reasoning_content`. Los cinco eran de código (C1 Rust ISO-8601, C3 `DeepReadonly<T>`, C5 Python
top-N, N1 Zig, N3 Swift 6). No es un fallo del servidor ni del cliente: el modelo entra en deliberación
circular sobre requisitos ambiguos del enunciado.

Se relanzaron los cinco con `max_tokens 16384`:

| prompt | tokens generados | razonamiento | resultado | tiempo |
|---|---|---|---|---|
| C1 (Rust) | 16 384 (tope) | 56 033 car. | **vacío** | 670 s |
| C3 (TypeScript) | 16 384 (tope) | 63 744 car. | **vacío** | 674 s |
| N1 (Zig) | 16 384 (tope) | 59 744 car. | **vacío** | 676 s |
| C5 (Python) | 3 325 | 11 991 car. | correcto | 130 s |
| N3 (Swift 6) | 8 013 | — | correcto | 414 s |

Es decir: **ampliar el presupuesto no arregla el bucle**, sólo lo hace más caro. Tres prompts consumieron
11 minutos de GPU cada uno para devolver cadena vacía. Leyendo la cola del razonamiento de C1 se ve el
patrón exacto: el modelo descubre que ISO 8601 admite años y meses, que no tienen duración fija en segundos,
y se queda alternando entre «devolver None» y «asumir 365/30 días» sin decidir nunca — literalmente
«*Which is more common in coding challenge? ... Hmm. Need choose*» repetido durante 56 000 caracteres.

**Qué NO lo arregla:** añadir al prompt «si algo es ambiguo elige lo más razonable y responde ya, no
deliberes». Se probó con C1: 208 s, 14 300 caracteres de razonamiento, respuesta vacía otra vez. La
instrucción en lenguaje natural no corta el bucle.

**Qué SÍ lo arregla:** desactivar el razonamiento en la propia plantilla de chat.

| variante | tiempo | razonamiento | respuesta | ¿compila? |
|---|---|---|---|---|
| por defecto | 670 s | 56 033 car. | vacía | — |
| `reasoning_effort: "low"` | 91 s | 5 478 car. | 1 921 car. | **sí, y pasa los tests** |
| `chat_template_kwargs: {"enable_thinking": false}` | **35 s** | 0 | 3 194 car. | **sí, y pasa los tests** |

Con `enable_thinking: false` los tres prompts colgados salen a la primera: C3 en 14,6 s, N1 en 20,5 s,
N3 en 14,3 s. **19× más rápido que el bucle, y con respuesta.** Regla operativa para clientes de este
servidor: en tareas de código con requisitos apilados, mandar `enable_thinking: false`; el razonamiento
largo no está aportando calidad, está aportando riesgo de respuesta vacía.

### Veredicto del compilador (no de mi criterio)

- **C2 Rust, borrow checker** — compila. La explicación de aliasing XOR mutabilidad es correcta.
- **C4 TypeScript, `async function*` por lotes** — compila en `--strict` y **se ejecuta**: lotes
  `[1,2,3] [4,5,6] [7]` y `RangeError` con `n=0`, como se pedía.
- **C6 SQL, CTE + `LAG`** — se ejecuta en SQLite real; la variación interanual sale correcta (−46,39 %).
- **C5 Python** — ejecutado: top-N con desempate alfabético correcto y `[]` para `n<=0`.
- **C1 Rust ISO-8601** (tras `enable_thinking: false`) — compila y **pasa las 6 aserciones**:
  `PT1H30M15S`→5415, `P2DT3H`→183600, `P1DT2H3M4S`→93784, y `None` para `"hola"`, `"P"` y `""`.
- **C3 `DeepReadonly<T>`** (ídem) — compila, congela en profundidad (`Object.isFrozen` true en objeto
  anidado y en array) y, prueba negativa, **el compilador rechaza** `frozenConfig.name = "otro"`
  (TS2540) y `frozenConfig.nested.ports.push(9090)` (TS2339). El tipo hace lo que dice hacer.
- **N2 Go 1.23 range-over-func** — se ejecuta: iterador `iter.Seq` correcto por lotes.
- **N4 WGSL** — compilado sin avisos y **ejecutado en la 780M**: suma de 1 000 flotantes exacta.
- **N1 Zig** — **falla al compilar en Zig 0.15.1**: `std.io.getStdIn` ya no existe. Pero el modelo
  **avisó él mismo** de que su código era para 0.13.x y de que «en Zig 0.14+ la API de `std.io` cambió»,
  que es exactamente lo que el prompt le pedía declarar. Fallo de conocimiento actualizado, no de honestidad:
  no se inventó una API que no existe, dijo para qué versión escribía.

8 de 9 bloques de código pasan el compilador; el noveno falla por versión y viene etiquetado como tal.
> ⚠️ **Matizado por [H-023](#h-023--el-verificador-ejecutaba-codigo-del-llm-sin-aislar-y-compila-no-era-funciona-2026-09-10):** "pasan el compilador" es *todo* lo que aquella medición demostraba — rustc compilaba como librería sin ejecutar nada, así que una función con la lógica invertida aprobaba igual. Con banco de pruebas y ejecución real, lo verificado es 3 PASA LAS PRUEBAS / 1 compila sin pruebas / 0 fallos.

### Idiomas e instrucciones

- **Castellano**: corrigió las 8 faltas del texto de taller sin tocar el estilo («Haber→A ver», «bamos→vamos»,
  «ha Bilbao→a Bilbao», «nos a pedido→nos ha pedido», «sino→si no», «abra→habrá», «asta→hasta»).
- **Inglés y chino**: correctos, incluida la traducción técnica del aviso de horno y los tres puntos sobre
  apantallamiento dentro del límite de caracteres.
- **Instrucciones literales**: acertó las 5 líneas de 1-2-3-4-5 palabras; saltó la Tierra en la lista de
  planetas por diámetro sin dejar hueco en la numeración; y la frase de 12 palabras exactas sobre TIG salió
  con 12 palabras contadas.
- **JSON estricto**: los dos válidos, con tipos correctos y sin markdown alrededor (`1535.55` como float,
  fecha normalizada a `2026-09-04`).
- **Razonamiento**: cruce de trenes correcto (11:06, comprobado a mano en 11:06:24) y el problema de lógica
  resuelto bien por casos.
- **Conocimiento técnico**: la respuesta sobre F-CPU/PROFIsafe y la de por qué un MoE de 177B/3B activos
  genera más rápido que un denso de 27B (limita el ancho de banda de memoria) son ambas correctas.

**Veredicto.** Flash-Next se confirma como modelo productivo: idioma, instrucciones y JSON sin fallos, y
código que compila. El único defecto real de esta tanda es el bucle de razonamiento, y tiene arreglo por
parámetro, no por prompt.


## H-020 — Kernel 7.1.13 -> 7.2.4: la generacion sube ~3%, pero la comparativa no esta bien controlada (2026-09-10)

Actualizacion de Fedora 44 de `kernel 7.1.13-200.fc44` a `7.2.4-200.fc44` (39 paquetes, reboot).
Sin ROCm ni DKMS: `amdgpu` va en el kernel, asi que no habia modulos fuera de arbol que recompilar.
Los argumentos `amd_iommu=off ttm.pages_limit=32505856` se heredan de `/etc/default/grub` y siguen
en `/proc/cmdline` tras arrancar. El 7.1.13 queda como entrada de arranque de reserva.

Misma orden exacta antes y despues: `bench-context.py --tokens 4000 32000 100000 --passes 3`,
`cache_prompt=False`, Flash-Next UD-IQ4_XS, llama.cpp `311d421`, servicio sin reiniciar entre pasadas.

| tokens de prompt | pp 7.1.13 | pp 7.2.4 | tg 7.1.13 | tg 7.2.4 |
|---|---|---|---|---|
| 3.065 | 308,7 | 309,1 (+0,1%) | 26,01 | 26,45 (+1,7%) |
| 24.041 | 345,8 | 345,5 (-0,1%) | 22,27 | 22,93 (+3,0%) |
| 74.993 | 238,5 | 236,1 (-1,0%) | 14,49 | 14,93 (+3,0%) |

Prefill plano: las tres diferencias caben en el ruido. Generacion arriba entre +1,7% y +3,0%, en la
misma direccion en los tres puntos, con dispersion intra-punto por debajo del 0,5%.

**Limitacion que invalida atribuir la mejora al kernel.** Todo el bloque 7.1.13 se midio antes del
reboot y todo el 7.2.4 despues: el orden A/B no se alterno. Un +3% consistente es igual de compatible
con un estado distinto de la maquina tras arrancar limpia (fragmentacion de memoria, termica, cache de
pagina) que con el kernel nuevo. Para atribuirlo habria que alternar arranques 7.1.13 / 7.2.4 desde la
entrada de GRUB de reserva y repetir la tanda en cada uno.

**Conclusion util.** El kernel nuevo no rompe nada: servicios `llama-flashnext` y `brush-server` suben
solos, GTT sigue en 126976M y VRAM en 1024M, y el rendimiento no ha bajado. Eso es lo que la medida
demuestra. La cifra de +3% queda registrada como observada, no como ganancia demostrada del kernel.

La forma de la curva no cambia: de 3.065 a 74.993 tokens el prefill cae un 24% y la generacion un 44%,
igual que antes de la actualizacion.

## H-021 — El instrumental daba falsos verdes: smoke-test que no fallaba y barrido de ubatch que movia dos variables (2026-09-10)

Origen: auditoria externa del repositorio fijada en el commit `23cb014`. Tres de sus
objeciones sobre los scripts se comprobaron **ciertas leyendo el codigo y el proceso
en ejecucion**, no de oido. Se corrigen aqui.

### 1. `smoke-test.sh` aprobaba servidores rotos

La version anterior, ante un fallo, imprimia el error y **terminaba con codigo 0**.
Ademas comprobaba el resultado con `== *"391"*`, asi que `1391` pasaba, y si `content`
venia vacio caia a `reasoning_content`, de modo que el bucle de razonamiento de H-019
—donde la aplicacion cliente recibe **respuesta vacia**— contaba como aprobado.

Consecuencia real: el "smoke-test 391 correcto" que se anoto tras actualizar el kernel
**no demostraba** que el servidor respondiera bien.

Corregido: contrato explicito de salida (0 solo si todo pasa), comparacion de digitos
exacta (`digitos == ["391"]`), y `content` vacio es fallo con mensaje distinto segun
haya o no razonamiento. Verificado con cinco escenarios:

| escenario | exit esperado | exit obtenido |
|---|---|---|
| M5 sano | 0 | 0 |
| servidor inexistente | 1 | 1 |
| clave invalida | 1 | 1 |
| servidor que devuelve `1391` | 1 | 1 |
| `content` vacio + razonamiento (caso H-019) | 1 | 1 |

Los dos ultimos con un servidor HTTP de mentira, para provocar el fallo a voluntad.

### 2. `bench-ubatch.py` no aislaba ubatch, y podia dejar la produccion tocada

Dos defectos independientes:

- `set_ubatch()` aplicaba **el mismo valor** a `--batch-size` y `--ubatch-size` con dos
  `re.sub`. El barrido movia las dos variables a la vez: sus resultados no pueden
  atribuirse a ubatch.
- Editaba **la unidad productiva en sitio**, sin copia previa ni bloque de restauracion.
  Si moria a mitad, el servicio se quedaba con la configuracion del ultimo punto.

Reescrito: genera una unidad de pruebas aparte (`llama-flashnext-bench`, puerto 8081,
`Restart=no`), la productiva se **lee pero nunca se escribe**, `--batch-size` se fija
con `--batch` y solo se mueve `--ubatch-size`, y la limpieza y el rearranque de la
productiva van en un `finally`. Verificado en seco que para ubatch 512/1024/2048 el
`--batch-size` permanece en 4096.

### 3. La clave de la API estaba en la linea de comandos

`--api-key <clave>` se veia en `/proc/<pid>/cmdline`, legible por cualquier usuario
local. Migrado a `--api-key-file /etc/llama-server/api-keys.txt`.

Tropiezo durante el cambio, que se documenta porque es la trampa util: el fichero se
creo con el directorio `/etc/llama-server` en `700 root:root`, y el servicio corre como
`lasso` -> `failed to open file`, servicio en bucle de reinicio ~2 minutos. Arreglo:
`root:lasso` y `750` en el directorio, `600 lasso:lasso` en el fichero. Comprobado
despues: `argv` ya solo contiene `--api-key-file`, cero procesos con la clave en la
linea de comandos, y la autenticacion sigue devolviendo 401 sin clave y con clave falsa.

Leccion: al mover un secreto a un fichero, comprobar los permisos **del directorio**
que lo contiene con el usuario del servicio (`sudo -u <usuario> cat ...`), no solo los
del fichero.

## H-022 — Con `batch` fijo, la ventaja de `ubatch 2048` sobre 1024 desaparece: H-011 estaba contaminado (2026-09-10)

**Estado:** confirmado · primer barrido hecho con el instrumental corregido de H-021
(unidad de pruebas aparte, `--batch-size` fijo, produccion solo leida).

H-011 publicó que `ubatch 2048` era el óptimo, un **+2,8%** sobre 1024. Ese barrido usaba
el `bench-ubatch.py` viejo, que aplicaba **el mismo valor a `--batch-size` y a
`--ubatch-size`**. Es decir: el punto "1024" era en realidad *batch 1024 + ubatch 1024*, y
el punto "2048" era *batch 2048 + ubatch 2048*. No comparaba ubatch, comparaba dos
configuraciones enteras.

Repetido con `--batch-size 4096` fijo en los dos puntos, prompt real de 30.018 tokens,
2 pasadas, servicio reiniciado entre puntos:

| ubatch | batch | pp (t/s) | tg (t/s) |
|---:|---:|---:|---:|
| **1.024** | 4.096 | **350,5** | 22,20 |
| 2.048 | 4.096 | 346,5 | 22,08 |

El orden **se invierte**: 1024 sale 1,15% por encima de 2048, cuando lo publicado decía
que 2048 ganaba por 2,8%.

**Interpretación honesta, que es la que toca:** la separación (1,15%) es apenas el doble
de la dispersión entre pasadas del mismo punto (0,57%), con solo 2 pasadas. Eso **no
alcanza** para proclamar que 1024 sea mejor. La conclusión defendible es que **a `batch`
igual, 1024 y 2048 rinden lo mismo dentro del ruido**, y que el `+2,8%` de H-011 era un
artefacto de mover `batch` a la vez — atribuible al `batch`, no al `ubatch`.

Consecuencias:

- Se corrige la recomendación de `docs/servicio-systemd.md`: `ubatch 2048` se mantiene en
  producción **por continuidad y porque está medido en ese estado**, no porque sea mejor
  que 1024. Quien necesite margen de memoria puede bajar a 1024 sin coste medible.
- La tabla de H-011 queda **marcada como contaminada**, no borrada (metodología del
  laboratorio). Sus tres puntos no son comparables entre sí.
- Sigue en pie de H-011 lo único que no depende de la comparación: `ubatch 4096` **no
  arranca** con `lazy off` (OOM en carga).
- Pendiente: rehacer el punto 512 con `batch` fijo para completar la curva limpia.

**Lo que este hallazgo demuestra de fondo:** el defecto del instrumental no era teórico.
Invalidó una recomendación publicada de configuración. La auditoría externa acertó al
señalarlo, y acertó en el orden: primero el instrumental, después las optimizaciones.

## H-023 — El verificador ejecutaba codigo del LLM sin aislar, y "compila" no era "funciona" (2026-09-10)

**Que estaba mal.** Dos cosas, y la primera es de seguridad:

1. `verifica-codigo.py` cogia el bloque de codigo que devolvia el modelo y lo ejecutaba **con
   mi usuario, mi red, mi HOME y mis claves**. Un `rm -rf ~`, un `curl` a un servidor ajeno o
   una lectura de `~/.secrets/` habrian corrido sin obstaculo. No paso nada, pero eso es
   suerte, no diseno.
2. El veredicto era binario y enganaba: rustc compilaba con `--crate-type lib`, asi que una
   funcion con la logica invertida **aprobaba igual**. "8 de 9 compilan" no significaba que
   8 de 9 funcionaran.

**Que se ha hecho.** Cada verificacion corre ahora bajo `systemd-run` con usuario efimero
(`DynamicUser=yes`), sin red (`PrivateNetwork=yes`), sin HOME (`ProtectHome=yes`), sistema en
solo lectura (`ProtectSystem=strict`), `MemoryMax=2G`, `TasksMax=256` y `RuntimeMaxSec`. Y el
veredicto pasa a tener tres estados: **NO COMPILA / COMPILA PERO FALLA / PASA LAS PRUEBAS**,
con aserciones derivadas del enunciado de cada prompt.

**El sandbox se comprueba antes de usarlo.** El script no se fia de su propia configuracion:
al arrancar intenta salir a la red, leer `~/.secrets/m5-llama-api.key` y escribir en `/usr`.
Si alguna de las tres cosas **funciona**, aborta y se niega a ejecutar codigo del modelo.

| comprobacion | resultado |
|---|---|
| `getent hosts github.com` | falla (sin red) |
| `cat ~/.secrets/m5-llama-api.key` | Permission denied |
| `touch /usr/PWNED` | Read-only file system |
| usuario efectivo | `run-u402` (efimero, no `lasso`) |

**Validado con sabotaje deliberado.** Para comprobar que el banco de pruebas detecta logica
mala (y no solo errores de sintaxis), altere tres respuestas buenas: el Rust devolviendo
`Some(42)` fijo, el Python ordenando **ascendente** en vez de descendente, y el SQL cambiado
por `SELECT 1`. Las tres compilan sin una queja. Resultado: **las tres cayeron en COMPILA PERO
FALLA**, y las respuestas reales sin tocar dan PASA LAS PRUEBAS. Con el verificador viejo las
tres habrian aprobado.

**Trampas encontradas por el camino** (las tres costaron tiempo, van aqui para no repetirlas):

- `DynamicUser=yes` **implica `PrivateTmp`**, asi que un directorio de trabajo en `/tmp` no
  existe dentro del sandbox: todo fallaba con `226/NAMESPACE`. El area de trabajo va en
  `/var/lib/verif-sandbox`.
- `ProtectHome=yes` vacia `/home`, y eso **anula cualquier `BindReadOnlyPaths` cuyo origen
  este en `/home`** (`rc=203`, ejecutable no encontrado). Las toolchains viven en el HOME
  (`~/.cargo`, `~/.nvm`, `node_modules`), asi que se montan ro **en el host** hacia `/opt/tc`,
  fuera de `/home`. Evita copiar 3 GB.
- El `python3` del venv de Hermes enlaza a librerias del HOME: dentro del sandbox se usa
  `/usr/bin/python3`. `bwrap` no era opcion en este equipo: AppArmor tiene
  `apparmor_restrict_unprivileged_userns=1` y los namespaces sin privilegio estan cerrados
  (`setting up uid map: Permission denied`).

**Resultado sobre las respuestas reales de Flash-Next (bateria v2):** 3 PASA LAS PRUEBAS,
1 COMPILA (sin banco de pruebas), 0 fallos, y C1/C3 salen SIN RESPUESTA — que son exactamente
los dos casos de `content` vacio de [H-019](#h-019). Coherente con lo ya sabido.

**Consecuencia para las cifras publicadas:** la frase "8 de 9 bloques de codigo compilan de
verdad" queda **degradada a "compilan"**, que es lo unico que aquella medicion demostraba. Lo
que ahora se puede afirmar sobre ejecucion real es solo lo de la tabla de arriba.

## H-024 — El banco de pruebas daba por buenas respuestas invalidas y salia 0 siempre (2026-09-10)

**Estado:** corregido y verificado con 52 pruebas automaticas · origen: auditoria externa
del corte `35a2f26`, reproducida punto por punto en la maquina.

El instrumental tenia cinco defectos que hacian que **un fallo se registrase como exito**.
Ninguno era teorico: todos se reprodujeron antes de arreglarlos.

| Defecto | Sintoma reproducido |
|---|---|
| `espera_salud(puerto)` ignoraba el servicio consultado | Consultaba siempre `is-active llama-flashnext-bench`, incluso al restaurar produccion en el 8080. La restauracion podia darse por buena sin comprobar nada. |
| `mide()` no validaba la respuesta | Un cuerpo `{}` devolvia `(0, 0, 0)` sin excepcion y el llamador lo etiquetaba `ok`: una medida de **0 t/s** entraba en la tabla como valida. |
| Smoke test extraia digitos | `-391`, `No es 391`, `391%` y `391 unidades` **pasaban** como CORRECTO. Solo acertaba rechazando `1391`. |
| `AUTH` con el literal `***` | La cabecera enviaba `Bearer ***` en vez de la clave: contra un servidor con autenticacion, el smoke test fallaba siempre por un motivo falso. |
| Codigos de salida | Los tres scripts salian `0` con los fallos meramente impresos. `verifica-codigo.py` hacia `sys.exit(main())` con `main()` devolviendo `None`. Cualquier automatizacion los daba por verdes. |

**Arreglos.** `scripts/validacion.py` centraliza los contratos, y son **distintos por tipo
de prueba** — esto es un matiz de la auditoria que merece constancia: exigir
`finish_reason: "stop"` universalmente seria otro error, porque en una medicion con
`max_tokens` fijado la finalizacion por limite es el comportamiento correcto. La tabla de
contratos esta en [`metodologia.md`](metodologia.md). Ademas se separa **fallo del modelo**
de **error del banco**: un timeout, un sandbox que no arranca o un compilador ausente ya no
se cuentan como "NO COMPILA" (salida `3`, distinta del `2` de fallo del modelo).

**Reproducibilidad.** Se anaden calentamiento explicito registrado y descartado, JSONL
crudo por peticion (`warmup: true/false`), resumenes recalculables desde ese registro, y
[`../benchmarks/bateria-publica.json`](../benchmarks/bateria-publica.json), que sustituye a
la bateria que vivia en `private/` (en `.gitignore`) y hacia las pruebas irrepetibles desde
fuera. Datos sinteticos, sin claves ni informacion interna.

**Como se demuestra.** `tests/` levanta un servidor HTTP falso que imita a `llama-server` y
ejecuta los scripts **como procesos**, comprobando el codigo de salida ante `{}`, cuerpo sin
`timings`, contenido vacio con razonamiento, `-391`, `No es 391`, finalizacion por limite,
cuerpo no-JSON, error 500, salud mala y servidor sin autenticacion. 52 pruebas, todas en
verde. Los errores de la auditoria ahora **fallan como deben**.

Dos fallos los encontro la propia bateria mientras se escribia: el literal `Bearer ***` y
un `esperado` mal calculado en un prompt de razonamiento. Es el argumento a favor de probar
el instrumento antes de volver a medir con el.

**Pendiente, deliberadamente sin hacer:** no se ha lanzado la campana de `ubatch`
512/1024/2048 con 5 pasadas. El orden acordado es instrumento primero; mezclar los arreglos
con cambios de kernel, modelo, compilacion o parametros de produccion invalidaria la
comparacion.

---

## H-025 — Segunda auditoria: los contratos no cerraban los caminos que anunciaban

**Fecha:** 2026-09-10 · **Corte auditado:** `0c06767` · **Estado:** corregido, 95 pruebas
automaticas (37 de ellas fallan contra `0c06767` y pasan aqui)

La correccion de H-024 fue real pero incompleta: las 52 pruebas cubrian las regresiones
conocidas y no tocaban el verificador de codigo ni los errores de la aguja, asi que podian
estar **todas verdes conviviendo con aprobados falsos**. Una segunda revision externa
encontro 19 observaciones. Se reprodujeron todas antes de tocar nada. Lo que fallaba:

| Hallazgo | Que pasaba |
|---|---|
| Calentamiento fallido oculto | En `bench-ubatch.py` la excepcion solo se propagaba si la peticion fallida **no** era el calentamiento. Calentamiento roto + medidas buenas = punto `ok` y salida `0`, con el error visible solo en el JSONL. Nunca se pudo garantizar que las medidas fueran calientes. |
| Salud a medias | `espera_salud()` devolvia `True` en cuanto obtenia HTTP 200 y solo consultaba systemd si la peticion **fallaba**. Un endpoint sano daba `True` sin consultar ni una vez la unidad, que podia estar `failed`: confundia "algo responde en el puerto" con "he restaurado este servicio". |
| Contrato "exacto" no exacto | `if fin and fin != "stop"` deja pasar la **ausencia** de `finish_reason`, y `rstrip(".")` aceptaba `391.` y `391...` mientras la metodologia prometia `content.strip()` identico. Una prueba propia incluso codificaba el comportamiento permisivo. |
| Metricas imposibles | Se comprobaba el tipo numerico pero no finitud ni coherencia: pasaban `inf`, `NaN`, velocidades negativas o de cero, y recuentos no enteros o negativos. |
| `--needle` aprobaba prueba fallida | El resultado de la aguja no influia en el codigo de salida, que dependia solo de las medidas de rendimiento; y la aguja pasaba por el contrato de *generacion medida*, que admite `length` — justo el caso en que el modelo **no** ha llegado a decir la clave. La peticion fallida de aguja tampoco quedaba en el JSONL. |
| TypeScript con error de tipos aprobado | `tsc` salia con codigo 2 por un `TS2322` real, pero como `noEmitOnError` esta **desactivado por defecto**, emitia el `.js`; el verificador comprobaba que el `.js` existiera, lo ejecutaba, las aserciones pasaban y el veredicto era `PASA LAS PRUEBAS`. |
| SQL validado por subcadena | Se buscaba que la salida *contuviera* ciertos numeros, asi que `SELECT '2024 2025 2026 84000' AS basura;` aprobaba sin consultar una tabla. |
| Esperado incorrecto en la bateria | En `P-COD-SQL` el enunciado pide hornos con **mas de 2** lecturas (solo H1), el comentario `_por_que` lo explicaba bien, y `esperado_filas` incluia H2. Un esperado mal habria penalizado al modelo que respondiera bien. |
| Bateria publicada pero no conectada | `bench-calidad.py` y `verifica-codigo.py` seguian leyendo `private/prompts.json`, y este ultimo **al importar el modulo**: en una copia limpia del repo, `--help` reventaba con `FileNotFoundError`. Enunciados publicos si; flujo reproducible, no. |
| Fallo del banco como veredicto | Un `203/EXEC` (systemd no llego a ejecutar el binario) se contaba como `NO COMPILA`, sin haber compilado nada. |
| Aislamiento "verificado" sin verificar | En la autocomprobacion del sandbox, si los comandos de diagnostico **no arrancaban**, sus codigos distintos de cero se leian como "sin red", "secreto inaccesible" y "escritura protegida": una comprobacion que no pudo ejecutarse certificaba seguridad. |
| Campana de calidad siempre verde | `bench-calidad.py` salia `0` aunque las N peticiones fallaran con HTTP 500. |

**Arreglos.** El calentamiento tiene que completarse antes de contabilizar medidas
(reintento limitado, explicito y registrado; si no, se aborta el punto con salida propia).
`espera_salud()` exige **unidad activa Y endpoint sano**, las dos condiciones que anuncia.
`validacion.py` gana un contrato de **recuperacion** para la aguja, separado del de
generacion medida, que distingue "cito la clave en prosa" (fallo de formato) de "no la
recupero" (no leyo la ventana); y valida finitud, plausibilidad y recuentos enteros. Las
fases van marcadas en el JSONL (`calentamiento` / `medida` / `needle`) para que al recalcular
una peticion de recuperacion no se mezcle con el rendimiento. `tsc` se invoca con
`--noEmitOnError` y se respeta su codigo de salida. SQL compara **filas y valores**, nunca
subcadenas, con el esquema viajando en el caso. `carga_bateria()` es un cargador comun con
`--bateria`, la bateria publica es origen de primera clase y `--help` funciona sin
`private/`. Un fallo del lanzador levanta `ErrorBanco` en las dos ramas (con y sin sandbox),
y una herramienta ausente tambien: no son veredictos. La autocomprobacion del sandbox
devuelve **tres estados** y el inconcluso aborta en vez de aprobar.

**Que NO se ha hecho.** No se ha lanzado la campana de rendimiento, ni se han tocado
kernel, modelos, compilacion ni parametros de produccion. El servicio `llama-flashnext`
del M5 no se ha reiniciado. El orden sigue siendo el acordado: primero el instrumento.

**Limites de esta ronda, dichos claros.** Las operaciones de systemd de las pruebas estan
**simuladas**: no se ha certificado el aislamiento real en el M5, solo que la comprobacion
ya no aprueba lo que no pudo comprobar. TypeScript, Node y SQLite se ejercitan con las
herramientas reales del equipo de trabajo, no con las del M5. Que 37 pruebas nuevas fallen
contra `0c06767` demuestra que detectan estos fallos concretos; no demuestra que no queden
otros.

## H-026 — El banco suspendia codigo correcto y contaba mal los fallos

**Fecha:** 2026-09-10 · **Corte auditado:** `2919da9` · **Estado:** corregido

Tercera revision externa. Los tres bloqueos del barrido de ubatch (calentamiento
obligatorio, salud conjunta, validacion de metricas) quedaron confirmados como
corregidos y el piloto supervisado autorizado. Quedaban cuatro defectos en dos
zonas distintas, ninguna de ellas en el camino del barrido.

### A. La bateria publica y el verificador no hablaban el mismo protocolo

Las pruebas de `P-COD-PY` y `P-COD-TS` terminaban con `print('ok')` /
`console.log('ok')`, pero `v_python()` y `v_node()` exigen `PRUEBAS-OK` en la
salida para aprobar. Consecuencia: **una solucion correcta se puntuaba como
COMPILA PERO FALLA**. Reproducido ejecutando implementaciones correctas de
`media_movil` y `agrupaPor`: ambas terminan sus aserciones y aun asi suspenden.

El fallo es de coherencia interna, no de criterio: el marcador se unifico en
`PRUEBAS-OK` en la bateria publica. **No se elimino la comprobacion del
marcador**: salir con codigo 0 no acredita que las aserciones se ejecutaran (un
fichero de pruebas vacio tambien sale 0).

Alcance real: la tabla `PRUEBAS` que usan los casos privados (C1, C4, C5, C6) ya
emitia el marcador correcto, asi que **las puntuaciones de modelos ya publicadas
no estan contaminadas**; el desajuste estaba solo en la copia publica.

Por que se colo: la prueba desde copia limpia solo cubria SQL. Una prueba por
lenguaje que ademas comprueba las dos mitades (la solucion buena aprueba **y** la
mala suspende) lo habria detectado. Ahora existe.

### B. El JSONL del barrido de contexto no era uno a uno con las peticiones

**B1 — doble escritura.** `measure()` anotaba el fallo y lo propagaba; el
`except` del bucle escribia otra fila con el mismo error, ademas sin `fase`. Una
peticion producia dos registros: calcular la tasa de fallos contando filas la
inflaba al doble. Corregido dejando la escritura en un solo nivel (`anota`). Se
aniade una prueba estructural: `jsonl.write` debe aparecer **una sola vez** en el
fichero, para que la duplicacion no vuelva por otro camino.

**B2 — timeout sin registrar.** `measure()` tipaba `HTTPError` y `URLError`, pero
un `TimeoutError` directo (lo lanza el socket, no urllib) escapaba del `except`
que registra: la peticion fallida desaparecia del JSONL. Ahora se convierte en
`ErrorInfraestructura` y se registra por el mismo camino, conservando duracion,
fase e identificador. Un timeout es fallo de infraestructura, nunca del modelo.

### Verificacion

10 pruebas nuevas (105 en total). Contrastadas contra `2919da9`: **8 fallan con
el codigo viejo** y pasan con los arreglos. Las de codigo se ejecutan con `tsc`,
`node` y `sqlite3` reales.

### Limitaciones que siguen en pie

Las operaciones de systemd de las pruebas siguen **simuladas**: el aislamiento
real y el ciclo parada/arranque/restauracion del M5 solo se pueden certificar
ejecutando el piloto. No hay CI en GitHub Actions para estos commits, asi que los
resultados son locales y reproducibles, pero no visibles como ejecucion publica.

### H-026b — Tres ajustes tras el informe de cierre de `12c93ba`

**Fecha:** 2026-09-10 · **Estado:** corregido · Ninguno bloquea el piloto.

1. **Dependencia falsa de Node.** `test_python_bueno_aprueba_y_malo_suspende`
   llevaba `@unittest.skipUnless(TIENE_NODE)`. En un equipo con Python y sin Node
   la prueba se omitia sin motivo. Decorador retirado.

2. **La comprobacion textual no es garantia.** Contar que `jsonl.write` aparece
   una vez no impide que esa funcion se invoque dos veces por peticion. Se aniade
   `test_cada_peticion_correcta_deja_una_sola_fila`, que cuenta filas contra
   peticiones reales (1 calentamiento + 3 pasadas) y exige que no haya dos filas
   con la misma `(fase, pasada)`. La textual se conserva **degradada a
   complementaria**, con el limite escrito en su docstring.
   Para poder distinguir filas hizo falta registrar el indice de pasada: el JSONL
   ahora lleva `pasada`, que ademas es necesario para el barrido.

3. **Defecto propio: la suite ocultaba pruebas.** Al ir aniadiendo clases al
   final de los ficheros con `>>`, el bloque `if __name__ == "__main__"` quedo a
   MITAD de `tests/test_extremo_a_extremo.py` y de `tests/test_verificador.py`.
   Ejecutar el fichero directamente corria **14 de 25** pruebas y salia `OK`: un
   falso verde del mismo tipo que los que persigue este banco. Solo se detecto
   porque `discover` y la ejecucion directa daban numeros distintos. Bloques
   unificados al final y `LaSuiteNoPuedeOcultarPruebas` comprueba que en cada
   fichero hay exactamente un bloque `__main__` y que no queda codigo despues.

107 pruebas. Las dos nuevas fallan contra `12c93ba` y pasan con el arreglo.

**Sobre el alcance de las puntuaciones**, se adopta la redaccion del auditor por
ser mas precisa que la propia: *las puntuaciones obtenidas mediante las rutas
heredadas correctas no estan afectadas por este desajuste de marcadores de la
bateria publica*. No se ha reevaluado el historico, asi que no se certifica que
esten libres de cualquier otro problema. Matiz correcto del auditor: **C6 no usa
el marcador** `PRUEBAS-OK`, se valida comparando los resultados de la consulta
SQL; los que si lo usan son C1, C4 y C5.

### H-026c — Etiquetas de la ejecucion diferencial: la cifra "10" era imprecisa

**Fecha:** 2026-09-10 · Sin cambio de codigo. Correccion de una afirmacion propia.

El revisor objeta que *"diez pruebas no satisfactorias no equivalen a diez
defectos independientes"* y que la cifra debe quedar respaldada por el registro
de ejecucion. Tiene razon en lo primero y **la cifra que comunique era
imprecisa**. Ejecucion diferencial completa, registro en
`evidencias/diferencial-2919da9.txt`:

| Magnitud | Valor |
|---|---|
| Pruebas ejecutadas | 107 |
| Lineas `FAIL`/`ERROR` | 9 |
| Metodos de prueba distintos | 8 |
| `subTest` (un metodo, varios `id`) | 1 (`P-COD-PY`, `P-COD-TS`) |
| Clases afectadas | 3 |
| Defectos raiz | 6 (4 de H-026 + 2 de H-026b) |

De donde salio el 10: aquella pasada era una **seleccion de clases**, no
`discover`, y en ella tambien fallaba `test_el_bloque_main_va_al_final_y_solo_una_vez`.
Falla contra los ficheros de prueba ANTIGUOS, pero en el diferencial se copian
los ficheros de prueba NUEVOS al worktree, asi que sus bloques `main` ya estan
al final y la guardia pasa. El defecto es real; no pertenece a esta comparacion.
La magnitud correcta y comparable es **8 metodos distintos sobre 6 defectos
raiz**, y ninguna de las tres cifras (4, 8, 10) mide lo mismo.

**Comprobacion adicional a raiz del registro:** el volcado contiene lineas
`llama-flashnext en estado 'failed'`, que asustan al leerlas. Son **valores
inyectados** por los dobles de prueba: `test_instrumental` sustituye `bench.sh`
en `setUp` y lo restaura en `tearDown`, para ejercitar la espera de salud.
Verificado dos veces: (1) produccion en el M5 seguia `active` con
`ActiveEnterTimestamp` anterior a la ejecucion; (2) con las 107 pruebas
ejecutadas bajo un `PATH` **sin** `systemctl`, no aparece ningun
`systemctl: not found`, luego ninguna prueba invoca al systemd real.
Aun asi, ese texto en un registro es una trampa de lectura para quien audite.

### H-027 — Piloto supervisado de ubatch: el procedimiento funciona; dos defectos propios

**Fecha:** 2026-09-10 · Autorizado por el propietario. Un punto, ubatch 1024,
calentamiento separado y dos medidas. Evidencias en `evidencias/piloto-ubatch/`.

**Resultado del procedimiento (que es lo que se validaba):** correcto de punta a
punta. Secuencia observada desde fuera, por sondeo cada 20 s: produccion
`inactive` + banco `active` durante la medida; a las 21:38:37 CEST produccion
`active` y banco `inactive`; unidad del banco eliminada de
`/etc/systemd/system`; sin residuos. Duracion real ~8 min (4 min 42 s de banco,
48,1 GB de pico segun systemd).

Cuatro comprobaciones de restauracion, mas dos añadidas:

| Comprobacion | Resultado |
|---|---|
| Unidad activa | `active` + `enabled` |
| Endpoint sano | `/health` 200 |
| Modelo esperado | `qwen3.8-flash-next` |
| Smoke autenticado | responde `OK` |
| Sin clave (control negativo) | 401, como debe |
| Unidad productiva intacta | `mtime` 16:29:37, cinco horas ANTES del piloto |

**Defecto 1 — el piloto no midio el batch de referencia.** Se lanzo con
`--batch 2048`, pero produccion corre `--batch-size 4096 --ubatch-size 2048`.
Se pidio expresamente "conservar el batch de referencia" y no se hizo: el valor
se escribio a mano en vez de leerlo de la unidad. Consecuencia: **las cifras de
este piloto no son comparables con la linea base de produccion** y no deben
entrar en ninguna tabla de rendimiento. No invalida la validacion del
procedimiento, que era el objetivo. Correccion pendiente: que el script LEA
`--batch-size` de la unidad productiva y falle si no se le pasa un valor
coherente, en lugar de aceptar cualquier numero.

**Defecto 2 — `pasada` no llego a `bench-ubatch.py`.** H-026b añadio el indice
de pasada al JSONL de `bench-context.py`; `bench-ubatch.py` distingue las filas
por `fase` y `ts` pero **no** por indice. Con dos medidas aun se distinguen; con
cinco por punto en la campaña, no. Hay que portar el campo antes del barrido.

**Cifras obtenidas** (validas solo como prueba de que el banco mide algo
estable, NO como comparacion): prompt_n 30.018, pp 348,8 y 347,8 t/s (media
348,3; dispersion 0,30 %), tg 22,10 y 22,10 t/s. Sin conclusion de rendimiento:
un solo punto y con el batch equivocado.

**Nota de metodo:** `pgrep -f "bench-ubatch.py"` lanzado por SSH se encuentra a
si mismo en su propia linea de comando y devuelve "corre" indefinidamente. Se
uso `pgrep -af "[b]ench-ubatch"`. Vigilar procesos remotos asi da falsos vivos.

### H-028 — Repeticion del punto ubatch=1024 con el batch productivo

**Fecha:** 2026-09-10 · Instrumental: `13114f7` · Autorizado por el propietario

Repeticion del piloto de H-027 con el defecto ya corregido: `--batch` omitido,
leido de la unidad productiva. El log lo confirma en la maquina real:
`[i] batch FIJO en 4096 (leido de la unidad productiva)`.

**Medidas** (ubatch=1024, batch=4096, prompt_n=30.018, 1 calentamiento descartado + 2 medidas):

| pasada | pp t/s | tg t/s |
|---|---|---|
| 1 | 349,4 | 22,07 |
| 2 | 348,8 | 22,07 |
| media | **349,1** | **22,07** |

Dispersion de pp: 0,18 %. Las tres filas traen `pasada`, `batch_ref=4096` y
`comparable_con_produccion=true`.

**Lo que NO se puede concluir.** El punto medido con el batch equivocado
(2048) dio 348,3 pp; con el batch productivo (4096) da 349,1. La diferencia
es 0,23 %, por debajo de la dispersion entre pasadas del mismo punto. Caben
dos explicaciones y este dato no distingue entre ellas:

1. con ubatch fijo, `--batch-size` apenas influye en este modelo y longitud de
   prompt, porque el troceado real lo gobierna el ubatch;
2. el `--batch-size` de la unidad de banco no llego a surtir efecto.

La hipotesis 2 no queda descartada por la sustitucion en la unidad: el
barrido verifica que el texto contiene el valor pedido, no que el servidor lo
aplique. Pendiente de comprobar leyendo el parametro efectivo del propio
servidor (log de arranque o `/props`) antes de cualquier campaña. Hasta
entonces, **no se afirma nada sobre el efecto del batch**.

Sigue en pie lo dicho en H-027: un punto no es una campaña y estas cifras no
se comparan con las de `llama-bench` (pp512/tg128, contexto corto).

**Restauracion: 7/7.** Unidad `active` y `enabled`; unidad de banco eliminada
sin residuos; unidad productiva intacta (mtime 16:29, previo al piloto);
`/health` 200; modelo `qwen3.8-flash-next`; smoke autenticado con respuesta;
peticion sin clave rechazada con 401.

**Anomalia menor anotada:** el smoke pidio "di OK" y el modelo respondio en
portugues ("Tudo bem!...") con `max_tokens=8`. Servicio sano; es deriva del
modelo con presupuesto de tokens minimo, no fallo de infraestructura. El smoke
deberia comprobar que hay respuesta no vacia, no una cadena concreta.

**Metodo, tercera vez que muerde:** `pgrep -c "[b]ench-ubatch"` volvio a dar un
falso "terminado" (conto 0 con el proceso vivo, y en la misma pasada la salida
llego partida en dos lineas). La sonda fiable es
`pgrep -af "[b]ench-ubatch\.py" | wc -l`. Lanzar con
`nohup setsid ... < /dev/null &` si funciono: el ssh local volvio a cortarse a
los 180 s y el piloto sobrevivio, con toda la salida en `piloto2.log`.

### H-029 — La duda del batch, resuelta: los dos pilotos SI aplicaron su batch

**Fecha:** 2026-09-10 · Instrumental: sobre `b9d7be4`

H-028 dejo abierto si el `--batch-size` de la unidad de banco llegaba a
aplicarse. Resuelto leyendo el journal de los dos pilotos: el progreso de
`prompt processing` avanza **de 2048 en 2048** en el piloto 1 y **de 4096 en
4096** en el piloto 2. Los dos batches se aplicaron de verdad.

**Conclusion que ahora si se sostiene:** con ubatch fijo en 1024 y prompt de
30.018 tokens, pasar `--batch-size` de 2048 a 4096 no mueve el rendimiento
(348,3 vs 349,1 pp t/s, 0,23 %, por debajo de la dispersion entre pasadas).
El troceado que importa lo gobierna el ubatch. Sigue siendo **un punto**, no
una campaña: no se generaliza a otros ubatch ni a otras longitudes.

**Por que no se saca de `/props`:** llama-server no expone `n_batch` ahi
(verificado en el M5: solo `n_ctx`, `total_slots`, `model_*`). El journal es
la fuente disponible.

**Automatizado.** `batch_efectivo()` deduce el batch aplicado del paso entre
trozos y el barrido lo verifica en cada punto. Si no coincide con el pedido,
lo grita, lo acumula como incidencia de validez y **devuelve codigo 2**: un
punto medido con un batch que no se aplico ya no puede pasar por bueno.
Cuando el prompt es corto y no hay paso dominante devuelve `None` y no se
afirma nada, que es preferible a inventar una conclusion.

**Defecto propio cazado por las pruebas nuevas.** La primera version tomaba el
paso MINIMO, y el ultimo trozo es el resto del prompt (30.018 con batch 4096
deja 1346): habria marcado como "no comparable" un punto perfectamente valido.
Corregido a paso mas frecuente. Es el segundo caso en este repo en que la
prueba encuentra el fallo antes que la maquina, que es justo para lo que esta.

**Verificacion sobre datos reales, no simulados:** la funcion se ejecuto
contra los journals de las dos ventanas del M5 y devolvio 2048 y 4096
respectivamente, ambos coincidentes.

7 pruebas nuevas en `tests/test_batch_efectivo.py`, las 7 fallan contra
`b9d7be4`. Suite: **121 OK**.

### H-030 — La comprobacion de restauracion, como script versionado

**Fecha:** 2026-09-10 · `scripts/restauracion.sh`

Durante los pilotos, la comprobacion de "¿quedo produccion como estaba?" se
tecleaba a mano cada vez. Asi se colo un smoke que exigia la cadena literal
`OK` con `max_tokens=8`: el modelo respondio "Tudo bem!..." y parecio un fallo
del servicio cuando el servicio estaba sano.

**El criterio corregido:** el smoke de restauracion exige **respuesta no vacia
con finalizacion normal**, no una cadena concreta. Verificar la correccion del
contenido es trabajo de `smoke-test.sh`, que da presupuesto de tokens
suficiente; mezclar ambas cosas producia falsos rojos, y una comprobacion que
da falsos rojos acaba ignorandose, que es peor que no tenerla.

Ocho comprobaciones: unidad activa, unidad habilitada, unidad de banco
eliminada, sin fichero de unidad residual, `/health` 200, modelo esperado,
smoke autenticado, y como **control negativo** que sin clave devuelva 401.

**Verificado contra el M5 real:** 8/8 y codigo 0 sobre produccion sana (el
modelo respondio "Si", que con el criterio viejo habria sido rojo). Controles
negativos: con una unidad inexistente da 2 fallos y codigo 1; con un puerto
muerto, 4 fallos. Detecta lo que debe detectar.

### H-031 — Campana nocturna desatendida: build, especulacion, DPM y cache-ram con umbrales fijados antes de medir

**Fecha:** 2026-09-11 · `scripts/campana-nocturna.py` · evidencias en `evidencias/campana-20260910/`

Cuatro palancas medidas en una sola pasada desatendida de 25,6 min, con
produccion parada, cada configuracion como proceso hijo en el puerto 8081 y
la unidad productiva rearrancada en `finally` (gane quien gane). Los umbrales
de adopcion se escribieron en la cabecera del script ANTES de lanzar; la
vision (`--mmproj`) se mantuvo cargada en todas las configuraciones porque es
requisito de uso, no variable.

**F1 · build 311d421 vs df03399** (13 commits, 3 Vulkan: fusion topk_moe
#28422, matrices M pequena #28457, copias async). Alternado A,B,A,B, 6 pasadas
cortas + 2 largas por lado, `prompt_n` real 2.294 y 18.038:

| build | pp 2,3k | tg 2,3k | pp 18k | tg 18k |
|---|---|---|---|---|
| 311d421 | 330,8 | 26,74 | 360,5 | 23,85 |
| df03399 | 335,4 | 26,86 | 361,8 | 23,97 |

+1,4 % prefill, +0,4 % decode: **dentro de la dispersion** (las pasadas de
una misma build van de 331 a 345). Los tres commits Vulkan no mueven la aguja
en este modelo con RADV. Se adopta igual porque el umbral era "no empeora"
(motivo: ir al dia con upstream, no rendimiento).

**F2 · especulacion sin pesos extra.** `--spec-type draft-mtp` **no arranca**:
`context type MTP requested but model doesn't contain MTP layers`. El GGUF
UD-IQ4_XS de Unsloth **no lleva la cabeza MTP** (o llama.cpp aun no la lee
para Qwen3.8-Next): la conclusion de H-020 "Flash-Next tiene cabeza MTP" sale
de la ficha, no del fichero, y aqui se cae. `ngram-simple` arranca y da lo
esperado de un metodo sin modelo: acceptance 29,6 % solo en codigo (26,31 vs
27,65 t/s del control = **peor**, porque cada borrador rechazado cuesta un
paso de verificacion), 0 % en prosa/json/creativo (identico al control).
Ganancia prosa+codigo 0,976x → no adoptable.

**Lo que este resultado SI dice sobre MTP, con precision** (corregido, la
redaccion anterior daba a entender que la via estaba cerrada): MTP embebido no
disponible en el GGUF/build productivo. Unsloth publica cabezas sidecar en
`MTP/` (shared-Q8_0 = 2,79 GB); requieren una rama con soporte qwen4exp-MTP
(PR #28243, abierta y draft) y **no han sido evaluadas aqui**. Lo medido es
que el fichero que tenemos cargado no las lleva, no que el modelo no las
tenga.

**Y sobre el techo:** la ruta no especulativa se estabiliza en ~27 t/s y
parece limitada por ancho de banda. **No es el techo de throughput efectivo:**
la especulacion amortiza varias salidas por lectura de pesos, asi que un
borrador que acepte de verdad rompe ese ~27 t/s en lugar de rozarlo. El
`0,976x` de `ngram-simple` mide un borrador malo, no un limite fisico.

**F3 · DPM `auto` vs `high`** (3 peticiones cortas tras 45 s de reposo, ciclo
auto→high→auto): prompt_ms 820 / 816 / 821. Idle 6,6 W → 14,4 W (+7,8 W a
cambio de nada). Enunciado con el alcance que tiene: **no se detecto
penalizacion de prompt_ms atribuible al escalado DPM tras 45 s de reposo en
este ensayo.** No es "el escalado no existe": es que a escala de una peticion
y con este reposo no se vio, y por eso no se adopta (se pagaria la potencia
sin contrapartida medida).

**F4 · aplicado:** rebuild del arbol productivo a df03399 (restorecon
incluido) y `--cache-ram 4096 → 12288`. Smoke: respuesta no vacia con
`finish_reason=stop` ('391' a 17·23), control negativo 401, `/props`
`modalities.vision=true`. Produccion final: pp 336,2 · tg 26,86.

**cache-ram, dicho como toca:** unica palanca de capacidad adoptada; **su
beneficio no esta cuantificado**. Provisional hasta H-032 (issue #27148 y fix
#27624 abiertos). Los 17 GB libres medidos y las "~4 conversaciones de 33k en
cache en vez de 1" son aritmetica de capacidad, no una medida de ganancia. Y
un aviso de metodo: la ausencia de lineas de prompt-cache en el journal **NO
prueba inactividad**; lo que se mide es `cache_n`, TTFT y memoria.

**Lecciones:**
- Una capacidad declarada en la ficha del modelo (MTP) hay que comprobarla
  contra el GGUF concreto antes de planear en torno a ella.
- El estimador de tokens del relleno (1,78 tok/palabra) se queda **24 % corto**
  con este tokenizador (2.294 reales vs 3.000 pedidos). No invalida la
  comparacion (todas las configuraciones reciben el mismo prompt y se registra
  `prompt_n`), pero las etiquetas "3k/24k" del plan eran falsas: usar siempre
  el `prompt_n` real en tablas.
- Con umbrales escritos de antemano, tres de cuatro palancas quedaron en "no"
  y el informe lo dice sin que nadie tenga que defender la noche de trabajo.

#### Fallos de ingenieria de la campana

Las medidas de arriba se sostienen. El **instrumento** que las tomo, no: una
auditoria externa encontro seis defectos, todos verificados en el codigo, y
tres de ellos habrian pasado inadvertidos precisamente porque la campana salio
bien. Corregidos en `37ddb36` (instrumental) con pruebas en `d3b6cbe`.

| | Fallo | Por que importa | Donde se cierra |
|---|---|---|---|
| **A** | `restauracion.sh` l.48 leia la clave de `LLAMA_API_KEY=` en `/etc/llama-server/*.env`, pero la unidad arranca con `--api-key-file`. Coincidian por un `.env` residual. | El gate comprobaba el servicio con una credencial que el servicio no usa. Tras una rotacion daria falso rojo o **falso verde** segun que fichero se hubiera tocado. | `scripts/credencial.sh`: la credencial se deriva del `ExecStart` efectivo (`systemctl cat`) y viaja por el entorno; solo se imprime enmascarada. |
| **B** | `campana-nocturna.py::espera_prod` (l.433) devolvia True con `/health`=200 y consultaba `is-active` despues. | Cualquier proceso escuchando en el puerto daba verde a "produccion restaurada". `bench-ubatch.py::espera_salud` ya lo hacia bien: habia dos esperas y una estaba mal. | `scripts/salud.py::espera_servicio`, unica: unidad activa **y** `/health` 200 **y** modelo correcto en `/v1/models`. |
| **C** | `smoke_prod` (l.448) aceptaba contenido no vacio + `stop`; no comprobaba que 17×23 fuera `391`. | Gate de promocion insuficiente: un backend roto responde, pero mal (es el fallo de H-021 otra vez, en otro sitio). | Los gates son `restauracion.sh` (generico) + `smoke-test.sh` (exacto), llamados como procesos. El exacto fija ademas `enable_thinking:false`. |
| **D** | Al adoptar la build se compilo **encima** de `/models/llama.cpp`; el rollback restauraba solo la unidad. | La unidad restaurada seguia apuntando al mismo path, ya con el binario nuevo dentro. **Restaurar la unidad no es restaurar la build:** el rollback existia sobre el papel. | `scripts/builds.sh`: build por sha en `/models/llama-builds/<sha>/`, promocion por symlink y `volver`. El runner revierte las dos cosas. |
| **E** | Script de un solo uso: worktree reutilizado sin comprobar a que apuntaba, `bak-campana-20260910` cableado, baseline `311d421` a mano, produccion **arrancada siempre** al final aunque empezara parada, y sin pruebas. | Cambiar el estado de la maquina por tu cuenta no es restaurar. Y sin pruebas, cada campana reestrena los fallos de la anterior. | `scripts/campana.py`: todo parametrizado, `estado_inicial` registrado y respetado, backup `.bak-<run-id>`, fases enchufables. |
| **F** | `TOKENS_POR_PALABRA = 1,78` estimaba un **24 % corto** (2.294 reales vs 3.000 pedidos). | Las etiquetas "3k/24k" de las tablas eran falsas. No invalida la comparacion, pero si la lectura. | `scripts/prompts.py`: corpus medido con el tokenizador real, congelado con su sha256 y verificado antes de cada A/B. |

La leccion que engloba a las seis: **una campana que sale bien no valida su
propio instrumental.** A, C y D solo se habrian manifestado en el camino de
fallo, que esa noche no se recorrio.

### H-031b — Lo que queda abierto de H-031, y una referencia externa que no hemos medido

**Fecha:** 2026-09-11 · sin medidas propias nuevas · **todo lo de aqui esta
marcado como no medido aqui**

Cinco cosas que H-031 dejo enunciadas con mas seguridad de la que tenian. Se
separan en una entrada propia para que no se citen como resultado:

1. **MTP.** MTP embebido no disponible en el GGUF/build productivo. Unsloth
   publica cabezas sidecar en `MTP/` (shared-Q8_0 = 2,79 GB); requieren una
   rama con soporte qwen4exp-MTP (PR #28243, abierta y draft) y **no han sido
   evaluadas aqui**. Pendiente en H-034.
2. **Techo.** La ruta no especulativa se estabiliza en ~27 t/s y parece
   limitada por ancho de banda. **No es el techo de throughput efectivo:** la
   especulacion amortiza varias salidas por lectura de pesos.
3. **cache-ram.** Unica palanca de capacidad adoptada; **su beneficio no esta
   cuantificado**. Provisional hasta H-032 (issue #27148 y fix #27624
   abiertos). La ausencia de lineas de prompt-cache en el journal **no prueba
   inactividad**: se mide `cache_n`, TTFT y memoria.
4. **DPM.** No se detecto penalizacion de `prompt_ms` atribuible al escalado
   DPM tras 45 s de reposo en este ensayo.
5. **Instrumental.** Los seis fallos A-F de la tabla anterior, cerrados en
   `37ddb36`/`d3b6cbe`, pero **sin ejecutar todavia una campana completa con
   el runner nuevo contra el M5**. El mecanismo esta probado contra un M5 de
   mentira; la primera campana real sera H-032.

#### Referencia externa: `drluoto/llama.cpp`, rama `strix-halo-vulkan`

**No medido aqui. Ninguna cifra de este bloque es nuestra.** Se anota porque
es el mismo hardware —Bosgame M5, Radeon 8060S, RADV— y porque su punto de
partida coincide con el nuestro: **sin especulacion, 27 t/s**. Commit
`ba5354d`, documentado en su `docs/strix-halo-flash-next-vulkan.md`.

Su pila combina requant de los densos a Q5_K con routers en Q8_0, una cabeza
de borrador `frspec-65k` propia, `GGML_VK_DISABLE_GDN_CACHE_FUSION=1`, y
`-np 3 --ctx-checkpoints 8`. Con eso reportan (repito: **cifras suyas, no
verificadas por nosotros**):

| Magnitud | Cifra reportada |
|---|---|
| decode, warm | 33,1 t/s |
| decode, codigo @8k | 41,6 t/s |
| decode, reescritura @8k | 55,4 – 63,2 t/s |
| prefill @8k | 340 → 510 t/s |
| prefill @32k | 280 → 390 t/s |

(En su tabla de agentes esos dos ultimos aparecen como 517 y 391.)

Dos avisos antes de que a nadie se le ocurra clonarlo:

- **No documentan `--mmproj`.** La vision es requisito de uso aqui, no
  variable, asi que una pila sin ella no es sustituible por la nuestra
  aunque sus numeros sean mejores.
- **Cantera de parches, no checkout.** Lo util es evaluar sus cambios **uno a
  uno** contra nuestra linea base (eso es H-036), no adoptar un arbol entero
  cuyo comportamiento con nuestro GGUF, nuestra cuantizacion y nuestra
  configuracion de vision no conocemos. Cambiar diez cosas a la vez y medir
  una mejora no dice cual de las diez la produjo — y es justo el error que
  H-022 nos costo desmontar.

### H-032 — La cache de prompt en RAM devuelve lo que guardo (y `-np 2 -kvu` no pierde prefill aqui)

**Fecha:** 2026-09-11 · `scripts/fases_h032.py` vía `scripts/campana.py` · evidencias en `evidencias/h032-20260911/`

Deuda de H-031: `--cache-ram 12288` se adopto sin medir. Tres fases con umbrales
escritos antes de medir, produccion parada 30 min, corpus congelados contra el
tokenizador real (`benchmarks/corpus/`, `prompt_n` verificado en la peticion fria).

**Correccion (issue #27148).** 4 conversaciones de ~2k tokens con un codigo de 12
hex cada una; 30 ciclos de 2 peticiones **concurrentes** preguntando por el codigo,
con `-np 2 -kvu` y `cache-ram` 0 / 4096 / 12288:

| cache-ram | peticiones | contaminaciones | no recupera | cache_n medio | TTFT frio | TTFT caliente | RSS |
|---|---|---|---|---|---|---|---|
| 0 | 60 | **0** | 0 | 746 | 6.128 ms | 572 ms | 28,2 GB |
| 4096 | 60 | **0** | 0 | 883 | 6.156 ms | 572 ms | 28,3 GB |
| 12288 | 60 | **0** | 0 | 1.019 | 6.131 ms | 575 ms | 28,1 GB |

180/180 correctas. El bug de #27148 (contexto de otra conversacion bajo carga
concurrente) **no se reproduce** en esta configuracion. Dos matices: la
reproduccion publicada es sin `-kvu` y aqui se probo la unidad real (con `-kvu`);
y `cache-ram 0` tambien acierta cache porque el KV del slot sigue vivo entre
peticiones (`cache_n` 746), asi que la cache RAM solo aporta cuando el slot se
reasigna a otra conversacion.

**Regresion #28495 (hipotesis: reportada en HIP/ROCm).** 4 peticiones consecutivas
de 16.362 tokens, `cache_prompt=false`, mediana de la 2ª-4ª vs la 1ª:

| config | pp 1ª | mediana pp 2ª-4ª | ratio | tg |
|---|---|---|---|---|
| np=1 | 350,9 | 349,0 | 0,994 | 24,1 |
| np=2 -kvu (produccion) | 349,3 | 348,0 | 0,996 | 24,1 |
| np=2 sin kvu | 357,2 | 355,4 | 0,995 | 24,0 |

**No hay regresion en Vulkan/RADV**: la caida del 42-54 % de #28495 es de los kernels
FA de CUDA/HIP con KV unificado, como decia el propio issue. Dato colateral:
`-np 2` sin `-kvu` prefill un 1,9 % mas rapido que con `-kvu`; dentro de lo
esperable (el KV unificado paga algo en atencion) y no compensa perder la
comparticion de KV entre slots.

**Rendimiento 4096 vs 12288.** TTFT de la 2ª peticion a un mismo contexto de 8.159
tokens, 5 repeticiones: 572,10 vs 572,13 ms (ratio 1,0001), `cache_n` 8.155 en
ambas, RSS identica. **Adoptado 12288** por el criterio escrito (≤1,05x): no cuesta
nada y admite mas conversaciones calientes. Lo que NO mide esta fase: el
beneficio con >2 conversaciones largas alternandose (ahi es donde 12 GB frente a
4 deberia notarse), que es el caso de uso real con varios clientes. Pendiente.

**Correccion a H-031:** la frase "la cache fue la unica palanca que sirvio" pasa a
"la cache es segura (0 contaminaciones en 180 peticiones concurrentes) y no cuesta
TTFT; su beneficio de capacidad sigue sin cuantificar".

### H-033 — PR #28501 (row-id hoisting para 512 expertos): +16 % / +13 % de prefill, salida greedy identica

**Fecha:** 2026-09-11 · `scripts/fases_h033.py` · evidencias en `evidencias/h033-20260911/` · parche `evidencias/parches/pr28501-vulkan-266464c8.patch`

A/B alternado baseline, candidato, baseline, candidato (2 rondas), misma linea de
argumentos productiva, `cache_prompt=false`, 160 tokens generados. Candidato =
df03399 + los dos ficheros Vulkan de la PR #28501 en su head 266464c8 (el hunk de
`tests/test-backend-ops.cpp` no aplica sobre df03399 y no afecta al binario). Cada
build en su directorio `/models/llama-builds/<sha>[+<sha256 parche>[:8]]` con
`manifest.json` (repo, sha, flags, compilador, hash del parche).

| prompt_n | pp base | pp cand. | ratio pp | tg base | tg cand. | ratio tg |
|---|---|---|---|---|---|---|
| 8.159 | 368,6 | **428,0** | **1,161** | 25,45 | 25,45 | 1,000 |
| 32.784 | 300,8 | **340,3** | **1,131** | 21,48 | 21,49 | 1,000 |

Greedy (3 prompts, seed 42, temperature 0): **contenido identico** entre brazos.
Umbrales (pp ≥1,05x en ambos tamanos, tg ≥0,98x, greedy identico): **pasa los tres**.
Coincide con lo que reporta el autor de la PR en Strix Halo (+19 % @8k, +16 % @32k)
y con la doc de drluoto (H-031b) para el UD-IQ4_XS stock.

**Desplegado en produccion el 2026-09-11 09:50** (ventana aparte, como pedia la
auditoria de P0): `ExecStart` migrado a `/models/llama-current/build/bin/llama-server`
(backup `.bak-symlink-20260911`), `daemon-reload` + `restart`, ~2 min de corte.
Verificado sobre el proceso vivo: `/proc/<pid>/exe` apunta a la build
`…+14eebc61`, `restauracion.sh` 8/8, `smoke-test.sh` 391 exacto + 401,
`modalities.vision=true`. Medida de produccion con el corpus de 8.159 tokens:
**pp 431,3 / 426,2 · tg 25,47** (antes 368,6 / 25,45). A partir de aqui promover
o revertir una build es `builds.sh promover|volver` + `systemctl restart`, sin
tocar la unidad.

**Lo que hizo falta arreglar para poder medir** (commit 4bf6d0d): la credencial
del gate se lee ahora del argv resuelto por `systemctl show`; el corpus se
verifica con la peticion fria (con cache caliente `prompt_n` cuenta solo lo
recomputado); SIGTERM pasa por los `finally`. Los tres fallos estaban en verde
con dobles y salieron a la primera contra la maquina real: los dobles se
corrigieron para que imiten al servidor de verdad.


### H-034 — Cabeza MTP sidecar a `np=1`: ×1,9 en codigo/JSON, 98k de contexto sin incidente, vision intacta; la igualdad greedy NO se cumple (2026-09-11)

**Pregunta**: la cabeza de borrador `MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf`
(Unsloth, 2,79 GB) sobre la rama con PR #28243 (qwen4exp-MTP) apilada a la build
productiva (`df03399` + PR #28501), ¿acelera la generacion en un solo slot sin
romper nada? Nada se despliega en esta fase: `np=1` no es produccion (eso es H-035).

**Metodo** (`scripts/fases_h034.py`, lanzado con `scripts/h034-medir.py`, que
para y rearranca `llama-flashnext` en un `finally` y NO promueve build): tres
brazos alternados (control sin MTP / MTP `--spec-draft-n-max 2` / n-max 3),
5 familias del corpus congelado (prosa 2.043 tok, codigo 77, json 106,
reescritura 586, creativo 41), 2 pasadas, `max_tokens 256`, greedy (seed 42,
temperature 0). Umbrales fijados antes de medir: tg mediana ≥1,15x, ninguna
familia <0,95x, pp ≥0,97x, salida greedy identica al control, y especulacion
demostrada (`draft_n` en `timings` o `draft acceptance` en el log). Luego
escalera 8k/32k/64k/98k con n-max 2 vigilando el kernel (DeviceLost RADV,
#27306) y exigiendo pp ≥0,9x la baseline de H-033 donde la hay; y una pregunta
de vision (PNG cuadrado rojo, `--mmproj`) en ambos brazos. Ventana de
produccion parada: 10:59–12:04 (65 min). Crudo en `benchmarks/h034/`.

**Velocidad (tg t/s, media de 2 pasadas; aceptacion del borrador entre parentesis):**

| familia | control | MTP n-max 2 | MTP n-max 3 |
|---|---|---|---|
| codigo | 27,65 | 46,41 (0,95) | **51,68** (0,94) |
| json | 27,68 | 47,22 (0,98) | **53,70** (0,99) |
| reescritura | 27,45 | 47,39 (1,00) | **53,52** (1,00) |
| prosa | 26,84 | 35,75 (0,58) | 36,31 (0,51) |
| creativo | 27,68 | 34,30 (0,56) | 28,96 (0,37) |

El prefill no baja: sube en todas las familias (p. ej. prosa 419 → 436). La
especulacion esta demostrada por las dos vias (`draft_n`/`draft_n_accepted` en
`timings` y `draft acceptance` en el log del banco). Donde el texto es
predecible (codigo, JSON, reescritura) la cabeza acierta el 94–100 % y la
generacion casi se dobla; en texto abierto (creativo) n-max 3 ya no compensa
(37 % de aceptacion). `nmax_recomendado = 3` por mediana, pero para un servidor
de uso mixto n-max 2 es el brazo que no pierde en ninguna familia.

**Escalera de contexto (n-max 2, `np=1`):**

| objetivo | prompt_n | pp | tg | aceptacion | ratio pp vs H-033 |
|---|---|---|---|---|---|
| 8.192 | 8.159 | 421,3 | 31,16 | 0,53 | 0,984 |
| 32.768 | 32.784 | 316,2 | 27,40 | 0,56 | 0,930 |
| 65.536 | 65.547 | 206,2 | 23,19 | 0,63 | (sin baseline) |
| 98.304 | 98.310 | 151,8 | 20,34 | 0,56 | (sin baseline) |

Los cuatro escalones terminan con `finish_reason stop`, `/health` 200 y cero
lineas de amdgpu/DeviceLost en el kernel: **techo probado 98.304 sin incidente**.
Nota: la baseline de H-033 se midio a `np=2 -kvu` (produccion); aqui es `np=1`,
asi que el ratio pp es orientativo, no A/B estricto.

**Vision**: control y candidato-MTP responden "Rojo" con `stop`. La cabeza no
rompe `--mmproj`.

**Lo que no pasa: la igualdad greedy.** En 10 de las 20 comparaciones el
`content` del brazo MTP difiere del control (2 familias en n-max 2 —creativo y
prosa— y las mismas mas codigo en n-max 3, en las dos pasadas cada una; la
repeticion identica entre pasadas indica que es determinista, no ruido). Los
primeros 80 caracteres coinciden siempre; la divergencia esta mas adentro. Con
verificacion exacta la salida especulativa deberia ser identica token a token, asi
que hay dos hipotesis abiertas: (a) deriva numerica del lote de verificacion
(batch de n+1 tokens vs 1 token) sobre Vulkan/RADV, que cambia el argmax en
empates cercanos —esperable y benigno—, o (b) la PR draft #28243 acepta tokens
que no verifica —inaceptable. **Fallo instrumental propio**: la fase guarda del
texto solo los 80 primeros caracteres, de modo que no se puede localizar el punto
de divergencia a posteriori; H-035 debe guardar el `content` completo y los
logprobs del primer token distinto para decidir entre (a) y (b).

**Veredicto**: fases escalera y vision `adoptar=True`; fase velocidad
`adoptar=False` por el gate greedy (el umbral se fijo antes y se respeta).
Nada desplegado. Antes de H-035 (MTP a `np=2 -kvu`, donde si se despliega):
cerrar la igualdad greedy con contenido completo, y decidir n-max 2 vs 3 con
el mix real de peticiones.

**Instrumental** (commit `af65099`): `fases_h034.py` (3 fases), `banco.py`
con `--spec-type draft-mtp -md`, `salud.py` con vigilancia del kernel,
`banco_falso.py` con MTP simulado (aceptacion, factor por familia, greedy
distinto, vision), 27 pruebas nuevas que fallan todas contra el commit anterior
(`git worktree --detach 1b7a935`). Bug real cazado por las pruebas antes de
tocar el M5: `_corpus_de_la_escalera` no devolvia el corpus.
