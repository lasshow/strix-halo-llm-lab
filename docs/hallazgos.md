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
