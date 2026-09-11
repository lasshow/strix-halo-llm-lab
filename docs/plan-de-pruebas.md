# Plan de pruebas del laboratorio — M5 (Strix Halo, 124 GiB unificados)

> Hoja de ruta viva. Cada prueba entra de una en una, se mide en esta máquina y se
> documenta en [`hallazgos.md`](hallazgos.md) antes de pasar a la siguiente.
> Última revisión: 2026-09-09.

## Principios (los que ya nos han mordido)

1. **Decide `llama-server`, no `llama-bench`** (H-004/H-005, H-011: tres discrepancias).
2. Una prueba a la vez, con el servicio de producción restaurado y verificado al acabar.
3. Cifras de comunidad sin enlace = no verificadas. Aquí solo se publican medidas propias.
4. En esta máquina mandan los **~GB/s de memoria**, no los TFLOPS: MoE grande ≫ denso mediano.

---

## Fase A — LLM de texto (en curso)

| # | Prueba | Estado | Nota |
|---|--------|--------|------|
| A1 | Barrido ubatch con `lazy off` | ✅ H-011 | 2048 óptimo; 4096 no arranca |
| A2 | Contexto largo hasta 98k + aguja | ✅ H-012 | tg −50% por KV; aguja 6/6 |
| A3 | **Qwen3-Next-80B-A3B IQ4_XS (42,6 GB)** | ✅ hecho ([H-013](hallazgos.md), [ficha](../benchmarks/qwen3-next-80b.md)) | Respuesta: 1,5-2x más rápido, generación casi plana hasta 122k (atención híbrida), mitad de memoria, 6/6 agujas. Falta la batería A8 para decidir promoción. Pregunta: ¿cuánta calidad/velocidad compra 512→512 expertos con 177B→80B totales? Y de paso: con 42 GB libres de sobra, ¿mejora tg por menos presión de memoria? |
| A4 | 131k reales y 262k punta a punta | pendiente | El estimador se quedó corto (98k al pedir 131k). Recalibrar y empujar hasta la ventana entera |
| A5 | GLM-5.3-Flash **REAP50-IQ4_XS (88 GB)** | pendiente | 50% expertos podados + cuantización decente vs el IQ1_S ya medido (8,3 t/s) |
| A6 | Actualizar llama.cpp (≥15 commits) y re-medir A1 | pendiente | El fix del lazy-mode por defecto ya está upstream; verificar que no cambia nada más |
| A7 | **DeepSeek V4-Flash** | bloqueada | 284B a 4 bits ≈ 145 GB: **no cabe**. Vías: poda `reap-150b` (~80 GB a 4 bits) o UD-Q2_K_XL. V4.1-Flash aún no existe en HF (comprobado 09-09): al radar |
| A8 | Batería de calidad fija entre modelos | pendiente | Mismo set de ~20 prompts (código, extracción, razonamiento, castellano) para comparar modelos con algo más que la aguja |

**Regla de decisión de A3:** si el 80B da calidad comparable en la batería A8 con tg
sensiblemente mejor, se convierte en candidato a producción y libera ~45 GB para la Fase B
sin parar el servicio. Ese es el premio real de la prueba.

---

## Fase A2 — Cola de campañas sobre el modelo en producción (H-032 … H-036)

Todas se ejecutan con el runner versionado [`../scripts/campana.py`](../scripts/campana.py),
una por ventana, con los umbrales escritos **antes** de medir y los dos gates
(`restauracion.sh` genérico + `smoke-test.sh` exacto) como condición de promoción.
El corpus sale de [`../scripts/prompts.py`](../scripts/prompts.py): **el mismo fichero
congelado para todos los brazos**, verificado contra `timings.prompt_n` antes de cada A/B.
Cada fase levanta su servidor de banco con
[`../scripts/banco.py`](../scripts/banco.py), que deriva la línea de arranque del
**ExecStart real de la unidad** (nunca una línea copiada a mano), mantiene la credencial
fuera de `argv` y mata el proceso en el `finally` —SIGTERM, SIGKILL a los 20 s y espera a
que el puerto quede libre—, porque un servidor de banco mal apagado es lo que hacía que
"producción restaurada" diera verde en H-031.

Van en este orden a propósito: H-032 cierra la deuda que dejó H-031 (se adoptó
`cache-ram 12288` sin cuantificar su beneficio), y H-034/H-035 dependen de que
H-032 no haya encontrado corrupción de caché.

### H-032 — ¿La caché de prompt en RAM devuelve lo que guardó?

Primero **corrección**, después rendimiento: una caché que acelera y contesta con el
contexto de otra conversación no es una optimización, es un fallo de datos servido
rápido. Es la deuda directa de H-031, que adoptó `--cache-ram 12288` con el beneficio
sin cuantificar (issue #27148, fix #27624, ambos abiertos).

Implementada en [`../scripts/fases_h032.py`](../scripts/fases_h032.py), tres fases
enchufables e independientes. Baseline y candidato son la **misma build** (df03399):
aquí no se compara código, se compara configuración.

```
python3 campana.py --run-id h032 --baseline-sha df03399… \
    --fase fases_h032.py:correccion_cache_ram \
    --fase fases_h032.py:regresion_28495 \
    --fase fases_h032.py:rendimiento_cache_ram
```

- **Correctness** (`correccion_cache_ram`): *nonces* de 12 hex sembrados en 4
  conversaciones A-D (cada una con ~2k tokens de corpus congelado delante, para que el
  prefijo largo sea lo que la caché guarda) y recuperados después; además **pares
  concurrentes** elegidos al azar, que es donde una caché compartida entre slots puede
  cruzar contextos. 30 ciclos por defecto, no tres.
  Contrato por respuesta ([`recuperacion_nonce`](../scripts/validacion.py)): el contenido
  normalizado contiene su nonce y **ninguno** de los otros tres —incluido el de la
  petición que viaja en paralelo—. Se distingue *contaminación* (aparece un nonce ajeno)
  de *no recupera* (no aparece ninguno): solo la primera descalifica `--cache-ram`.
- **Barrido:** `cache-ram` 0 / 4096 / 12288, con `-np 2 -kvu`.
- **Qué se mide, y no "si aparecen líneas en el journal":** `cache_n`, TTFT con caché
  (2ª petición a la misma conversación) frente a sin caché (1ª), `prompt_n` real y RSS
  del proceso (`/proc/<pid>/status VmRSS`). La ausencia de líneas de prompt-cache en el
  journal **no prueba** inactividad.
- **Regresión #28495, como HIPÓTESIS y no como fallo demostrado** (`regresion_28495`).
  El reporte fuerte es sobre HIP/ROCm y nosotros somos Vulkan/RADV: puede no aplicarnos
  en absoluto, y por eso esta fase es **diagnóstico y no vota**. Contraste `np=1` vs
  `np=2 +kvu` vs `np=2` sin `kvu`, 4 peticiones largas consecutivas (corpus 16.384),
  `cache_prompt=false`, **midiendo pp desde la 2ª** (la 1ª paga el arranque en frío y
  contamina la comparación).

**Umbrales de H-032, escritos antes de medir** (salen de los docstrings de
`fases_h032.py`; las constantes son `UMBRAL_CAIDA_PP` y `UMBRAL_TTFT`):

| Fase | Criterio | Efecto |
|---|---|---|
| `correccion_cache_ram` | **contaminaciones == 0 en TODAS** las configuraciones de `cache-ram` | `adoptar=True`. Una sola respuesta con nonce ajeno ⇒ `adoptar=False` y el detalle en `error`. No hay umbral estadístico: no se negocia una tasa de fuga de contexto |
| `correccion_cache_ram` | una configuración que **no se pudo medir** | tampoco acredita cero ⇒ `adoptar=False` |
| `regresion_28495` | mediana de pp de la 2ª-4ª **< 0,80×** la 1ª, **o** < 0,80× la mediana de `np=1` | `resumen.regresion_detectada=True`. No vota (no devuelve `adoptar`) |
| `rendimiento_cache_ram` | TTFT mediano de `12288` **≤ 1,05×** el de `4096` (5 repeticiones sobre contexto de 8.192 con `cache_prompt=true`, tras una de cebado) | `adoptar=True` y `aplicar` deja `--cache-ram 12288`; si no, lo baja a `4096` |

> **Limitación declarada:** no hay estado compartido entre fases (`Contexto` no lo tiene
> y no se toca `campana.py` para añadirlo). `rendimiento_cache_ram` no puede saber por sí
> misma si hubo contaminación: el voto combinado lo hace el runner (todas las fases con
> `adoptar` deben ser `True`), así que no se promueve nada, pero su `aplicar` escribiría
> `4096` en vez de apagar la caché. Para **apagarla** hay que ejecutar la campaña con
> `H032_CONTAMINACION=1`, y entonces `aplicar` escribe `--cache-ram 0`.

### H-033 — df03399 vs df03399 + PR #28501

A/B mínimo: el candidato es la baseline **más el parche de esa PR y nada más**, fijada
por **SHA exacto** (una PR es una rama móvil; "la PR #28501" sin SHA no es una
configuración reproducible). El `patch_sha256` queda en el `manifest.json` de la build.
Cualquier otra diferencia entre brazos invalida el punto.

Implementada en [`../scripts/fases_h033.py`](../scripts/fases_h033.py)
(`--fase fases_h033.py:ab_builds`). Orden **alternado A,B,A,B** —2 rondas × corpus 8.192
y 32.768, `cache_prompt=false`, `max_tokens 160`— porque medir A entero y después B
entero le regala al segundo brazo cualquier deriva térmica de la hora anterior. Misma
línea de argumentos productiva y mismo corpus congelado en los dos brazos.

**Umbrales de H-033, escritos antes de medir** (constantes `UMBRAL_PP` y `UMBRAL_TG`).
Adoptar exige las **tres** cosas a la vez:

| # | Criterio | Si no se cumple |
|---|---|---|
| 1 | `pp` candidato **≥ 1,05×** baseline **en los dos tamaños** | `adoptar=False`; una mejora que solo sale en un tamaño no es la que promete la PR |
| 2 | `tg` candidato **≥ 0,98×** baseline en los dos tamaños | `adoptar=False`; se admite un 2 % de ruido, no un peaje |
| 3 | **Igualdad greedy**: 3 prompts cortos con `temperature 0`, `seed 42`, `max_tokens 64` devuelven un `content` **idéntico** entre brazos | `adoptar=False` y el detalle en `error`: si el texto cambia no es una build más rápida, es otra build, y el A/B de velocidad ya no compara lo mismo |

No devuelve `aplicar`: aquí no hay nada que escribir en la unidad. La promoción de build
la hace el runner (`builds.sh promover`) cuando el voto sale `True` y los gates dan verde.

### H-034 — Cabezas MTP sidecar (la vía que H-031 no llegó a probar)

`MTP/` shared-Q8_0 (2,79 GB) de Unsloth, que necesita una rama con soporte
qwen4exp-MTP (PR #28243, abierta y draft).

Implementada en [`../scripts/fases_h034.py`](../scripts/fases_h034.py). Tres fases, todas
con `np=1` y sin `-kvu` (por #28286: `draft-mtp` con `--parallel > 1` contamina slots;
eso es H-035). La confirmación de que la especulación está activa es que `timings` traiga
`draft_n`/`draft_n_accepted` o que el log diga `draft acceptance = ...`: sin eso, la fase
reporta **error**, no mide "igual de rápido" sobre un servidor que no está especulando.

**Umbrales de H-034, escritos antes de medir** (constantes de `fases_h034.py`).

`mtp_np1_velocidad` — A/B/C alternado `control`/`n-max 2`/`n-max 3` × 2 pasadas × 5
familias (`prosa`, `codigo`, `json`, `reescritura`, `creativo`), `max_tokens 256`,
`cache_prompt=false`. Adoptar exige las **cinco** cosas a la vez:

| # | Criterio | Si no se cumple |
|---|---|---|
| 1 | mediana `tg` en **prosa+codigo+reescritura** **≥ 1,15×** el control para al menos un `n-max` | `adoptar=False`; si ninguno llega, no se recomienda ningún `n-max` |
| 2 | **ninguna** familia baja de **0,95×** el control | `adoptar=False`; una mejora de media que hunde una familia no es una mejora |
| 3 | `pp` **≥ 0,97×** el control | la cabeza ocupa memoria y toca el prefill: se admite un 3 % de peaje, no más |
| 4 | **igualdad greedy exacta**: el `content` de cada brazo `mtp-*` es idéntico al del control por familia y pasada | `adoptar=False` y `error` lo explica: la verificación de MTP es exacta, un texto distinto no es la misma salida más rápida |
| 5 | **especulación confirmada activa** en los brazos `mtp-*` | `adoptar=False`; medir dos brazos idénticos y anotar "no mejora" sería publicar una conclusión sobre MTP sin ejecutar MTP |

`mtp_escalera_contexto` — un solo servidor candidato, escalera **8k / 32k / 64k /
98k**, vigilando `dmesg` y reset de GPU en **cada escalón** (#27306: ya tuvimos un crash
de GPU real por prefill largo, H-014) y parando en el primero. Adoptar solo si los cuatro
puntos pasan sin incidente, con el servidor vivo y `/health` 200 después de cada uno, y
`finish_reason` en `stop`/`length`; donde hay baseline de H-033 (428 t/s @8k, 340 @32k) se
exige `pp` **≥ 0,90×**; para 64k y 98k no hay baseline y **no se fabrica una** (criterio:
solo "sin incidente y terminación normal"). Un `dmesg` ilegible es "punto sin vigilancia",
no "punto sano".

`mtp_vision` — control y candidato responden a la misma imagen PNG (cuadrado rojo, generado
en la fase con `zlib`+`struct`, sin PIL) con "rojo"/"red" y `finish_reason=stop`. Los dos
tienen que acertar: la visión es requisito de uso (`--mmproj` está en la línea productiva),
no una variable que se pueda apagar para que MTP luzca.

Ninguna fase devuelve `aplicar`: H-034 no despliega nada (`np=1` no es la configuración
productiva). "adoptar" aquí significa "la build MTP es segura y rápida a un slot"; el
despliegue con `np=2` es H-035.

### H-035 — MTP con `np=2` + visión (aquí sí se despliega)

**Solo si H-034 sale limpio.** Es la combinación que toca #28286; meterla antes de
tener H-034 en verde sería mover dos variables y no saber cuál rompió.

H-034 dejó una cosa sin cerrar: 10/20 divergencias greedy con solo 80 chars
guardados. `scripts/fases_h035.py`, lanzado con `scripts/cadena-h035.sh`
(baseline = `df03399+PR28501` = producción; candidato = +PR28243):

1. **`diagnostico_greedy`** (`np=1`): control y MTP con `logprobs: true,
   top_logprobs: 5`, content COMPLETO. En la primera posición distinta se mira
   la distribución del CONTROL: si el token que eligió MTP está a ≤ 0,5 nats del
   top1 → **empate numérico** (deriva del lote de verificación, benigno); si no
   está en el top-5 o está más lejos → **no_verificado** (la PR aceptó un token
   que el objetivo no elegía) y la fase tumba la adopción.
2. **`np2_kvu_aislamiento`**: MTP con la línea productiva, 4 conversaciones con
   nonce, 30 ciclos de pares concurrentes (contrato H-032). Una contaminación =
   #28286 nos toca = no se despliega.
3. **`np2_kvu_velocidad`**: A/B alternado control/MTP a `np=2 -kvu`, 5 familias,
   2 pasadas, **dos peticiones concurrentes** por familia. Umbrales: tg mediana
   ≥ 1,15x, familia ≥ 0,95x, pp ≥ 0,97x. Greedy **por logprobs** (a `np=2` ni el
   control es reproducible entre slots — 1ª pasada H-035): toda divergencia,
   intra-slot y MTP/control, debe ser empate ≤ 0,5 nats o de longitud;
   `no_verificado` tumba. Devuelve `aplicar`: si adopta, `banco.pon_mtp` escribe los 4 flags
   tras `--mmproj`; si no, `banco.quita_mtp` (idempotente con la unidad de hoy).
4. **`np2_kvu_vision`**: cuadrado rojo en ambos brazos y, en el MTP, visión y
   texto a la vez (los dos slots).

Despliegue: solo si las 4 adoptan, el runner escribe la unidad, promueve la
build y pasa `restauracion.sh` + `smoke-test.sh`; rojo = rollback de unidad y
build. Nota: el smoke exige `content == "391"` literal; con MTP una divergencia
tipo empate sobre ese prompt haría rollback — es el comportamiento seguro, no
un fallo del gate. `n-max = 2` (no pierde en ninguna familia en H-034).

### H-036 — Parches de `drluoto/llama.cpp`, uno a uno

Su rama `strix-halo-vulkan` es **cantera de parches, no checkout** (ver H-031b: mismo
M5 y mismo punto de partida de 27 t/s, pero sin `--mmproj` documentado y con la pila
entera cambiada a la vez). Se evalúa **un cambio por brazo** contra nuestra línea base:
requant Q5_K denso + routers Q8_0, cabeza de borrador propia,
`GGML_VK_DISABLE_GDN_CACHE_FUSION=1`, `-np 3 --ctx-checkpoints 8`. Adoptar diez cambios
juntos y medir una mejora no dice cuál la produjo — ese error ya costó desmontar H-011
en H-022.

### Y al final, el ubatch definitivo

`ubatch` **el último**, no el primero: el punto óptimo depende de la build, del
`batch`, de `cache-ram` y de si hay especulación. Rehacerlo antes de cerrar H-032…H-036
es medir una curva que va a moverse. Queda pendiente desde H-022 la comparación firme
1024 ↔ 2048 con calentamiento separado, 5 pasadas y orden equilibrado.

---

## Fase B — Imagen local (siguiente gran bloque)

Objetivo: generación/edición de imagen en el M5 sin nube. Aquí ya no es llama.cpp:
es PyTorch + ROCm sobre gfx1151, distinto terreno de juego.

| # | Prueba | Nota |
|---|--------|------|
| B1 | Stack base: PyTorch ROCm en Fedora 44 (o contenedor toolbox ya preparado para gfx1151) + ComfyUI | Primero smoke con SDXL (6,9 GB): tiempo/imagen 1024×1024, VRAM real |
| B2 | **FLUX.1-schnell** (~24 GB en fp8) | El estándar actual de calidad local rápida; 4 pasos |
| B3 | **Qwen-Image / Qwen-Image-Edit** | Edición instruida; interesante para el flujo AR/3D del taller |
| B4 | Convivencia con el LLM | ¿Caben Flash-Next (87 GB) + FLUX fp8 (~24 GB) a la vez? Si A3 promociona al 80B (43 GB), la respuesta es sí con margen |

## Fase C — Vídeo local (exploratoria)

| # | Prueba | Nota |
|---|--------|------|
| C1 | **LTX-Video** (ligero, ~13 GB) | El más realista para esta GPU; clips cortos 768×512 |
| C2 | **Wan 2.2** (14B / 5B) | Referencia de calidad open; medir si el tiempo/clip es tolerable en RDNA 3.5 |
| C3 | Interpolación/upscale (RIFE, Real-ESRGAN) | Barato y útil como postproceso de C1/C2 |

La memoria unificada es la baza: modelos de vídeo que revientan una 4090 de 24 GB caben
aquí. La incógnita honesta es el **tiempo de cómputo** (40 CU RDNA 3.5), no la memoria.

## Fase D — Voz (opcional, ya explorada en parte fuera de este repo)

ASR (Whisper/Parakeet) + TTS local como servicio auxiliar del M5. Baja prioridad:
el A8 ya cubre parte y no compite por la GPU.

## Radar (se revisa con el cron semanal de gfx1151)

- **DeepSeek V4.1-Flash**: no existe aún; cuando salga, evaluar tamaño/podas.
- GGUF nuevos de podas REAP de modelos >150B.
- Soporte `glm5next` upstream + ops fusionadas Vulkan (desbloquea re-medir GLM).
- Cambio de puerto por defecto de llama-server a :9931.
