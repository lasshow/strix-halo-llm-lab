# Forks de llama.cpp para Strix Halo: qué prometen y qué aplica a esta máquina

Evaluación **documental**, no medida. Aquí no hay ni una cifra propia: son las cifras que
publican terceros, leídas con la pregunta de si aplican a la configuración de esta máquina
(gfx1151, **Vulkan/RADV**, Qwen3.8-Flash-Next UD-IQ4_XS, 128 GB, kernel 7.2.4). El motivo de
no medir todavía está al final.

Consultado el 2026-09-10.

## Los dos candidatos

### `Aristo94/EngramHalo.cpp` (rama `strix-halo-qwen4exp`)

Apunta exactamente a este par máquina+modelo: Qwen 3.8 Flash-Next en Ryzen AI MAX+ 395.
Publica 24,4 → 39,3 tok/s en código y prefill a profundidad casi el doble (91 → 192 t/s
a 131K). Lo interesante es que documenta *por qué*: la atención QSA "sparse" corría en
realidad densa con una máscara (ancho de banda completo de KV a cualquier profundidad), el
`top_k` del indexador caía a CPU 12 veces por token pasados ~1K de contexto, y añade una
cabeza de draft MTP.

**El problema para mí está en su propio README, en el segundo párrafo:**

> **Backend scope: ROCm/HIP only.** […] On Vulkan/RADV this branch is reported to be a net
> loss: […] short-prompt prefill drop to roughly half of a stock build and the MTP path
> collapse to 6–7 t/s decode.

Mi build es Vulkan/RADV. Las 39,3 tok/s son de ROCm/HIP. Aplicado tal cual a esta máquina, lo
que su propia documentación predice no es una ganancia: es la mitad de prefill y un decode
peor que el actual (27,3 t/s → 6-7 t/s). Tampoco es una máquina equivalente: sus números son
de la variante de 96 GB.

Su README señala la vía que sí sería aplicable — y la señala él, no yo:

> porting just the MTP graph commits onto a Vulkan-tuned base keeps stock prefill and gains
> 26–35% decode with MTP

Es decir: **no adoptar la rama, sino trasplantar sólo los commits del grafo MTP** sobre una
base Vulkan. Y el propio texto marca ese dato como "unverified third-party report (Reddit)"
que ellos no han reproducido.

### `halo-box/strix-llama.cpp`

Más ambicioso de alcance ("la llama.cpp más rápida" para Strix Halo) y, al contrario que el
anterior, **recomienda Vulkan como camino por defecto**, que es el mío. Lleva cosas que sí
tocan mi configuración: chunking de mat-vec por lotes en RADV (`GGML_VK_MMV_NO_SPLIT=1` para
desactivarlo), padding de stride de LDS para coopmat, tabla de n-gramas en disco
(`--ngram-on-disk`, 28,8 GB en Flash-Next), y prefill especulativo.

El pero: **el README no publica una sola cifra**. Enumera cambios y flags, no resultados. Para
saber qué haría en mi máquina hay que compilarlo y medirlo — no hay número que contrastar.

Dos avisos suyos que valen aunque no toque el fork, porque son del chip y no del fork:

- En `gfx1151` hay un bug de corrección en ejecución asíncrona de HIP: la inferencia por lotes
  puede devolver salida muy incorrecta (perplejidad ~88 frente a ~9,4). Se mitiga con
  `HIP_LAUNCH_BLOCKING=1`, a coste de rendimiento. **Coincide con el P1 que ya vigilo**
  (issue #28211, logits erróneos en HIP sin crash). Un argumento más para seguir en Vulkan.
- Mat-vec por lotes a 3, 5 y 6 columnas es varias veces más lento que a 1, 2 y 4 en RADV, que
  es justo donde pega la decodificación especulativa (verifica exactamente a esos tamaños).

## Qué aplica a esta máquina

| | EngramHalo | halo-box |
|---|---|---|
| Backend que recomienda | ROCm/HIP | **Vulkan** (el mío) |
| Cifras publicadas | Sí, detalladas | **Ninguna** |
| Sobre Vulkan/RADV | "net loss" (su README) | camino por defecto |
| Máquina de referencia | 96 GB | no especificada |
| Aplicable directo aquí | **No** | Desconocido |

Ninguno de los dos es adoptable a ciegas. El de números concretos los da en un backend que no
uso y advierte de pérdida en el mío; el que apunta a mi backend no da números.

## Por qué no lo he medido todavía

Porque medirlo bien cuesta más que compilarlo, y hacerlo mal daría una cifra que luego habría
que corregir. Lo que exigiría una comparación honesta:

1. **Re-medir la tabla entera** con la build nueva (regla H-015): un fork cambia el entorno,
   así que las cifras actuales dejan de ser comparables.
2. **A/B alternado**, no dos bloques seguidos. Es el error que ya cometí en H-020 y que dejó
   ese hallazgo como observación en vez de ganancia.
3. **Perplejidad**, no sólo velocidad. El propio EngramHalo mide su reescritura de gather en
   wikitext-2 (0,03% de delta) porque es el único parche que toca la numérica del decode. Un
   fork que va más rápido y responde peor no es una mejora — y la batería de calidad (H-019)
   es la que lo detectaría.
4. **Sin tocar el servicio productivo.** El barrido de ubatch ya se hace sobre una unidad
   systemd aparte (H-021); un fork exige lo mismo: build en otro directorio, puerto distinto,
   `llama-flashnext` intacto.

Con eso en la mano, el orden defendible es: primero los **commits del grafo MTP sobre la base
Vulkan actual** (lo que el propio autor dice que conserva el prefill y gana 26-35% de decode),
y sólo después evaluar `halo-box` completo. Adoptar EngramHalo entero en Vulkan es lo único
que ya se puede descartar sin medir, y se descarta citando su propia documentación.

## Fuentes

- `https://github.com/Aristo94/EngramHalo.cpp` — rama `strix-halo-qwen4exp`
- `https://raw.githubusercontent.com/Aristo94/EngramHalo.cpp/strix-halo-qwen4exp/docs/strix-halo/README.md`
  — tabla de medidas, aviso de alcance ROCm/HIP, tabla de commits
- `https://github.com/halo-box/strix-llama.cpp` — README: cambios, flags, avisos de gfx1151
- `ggml-org/llama.cpp` PR #27742 (linaje qwen4exp), #27739 (diseño de referencia de MTP)
