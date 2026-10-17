<div align="center">

# 🔬 Strix Halo LLM Lab

**Cuaderno de laboratorio abierto: inferencia LLM local en un mini-PC AMD Strix Halo (128 GB unificados)**

[![Hardware](https://img.shields.io/badge/CPU-Ryzen%20AI%20MAX%2B%20395-ED1C24?style=flat-square&logo=amd&logoColor=white)](#hardware)
[![GPU](https://img.shields.io/badge/iGPU-Radeon%208060S%20gfx1151-ED1C24?style=flat-square&logo=amd&logoColor=white)](#hardware)
[![RAM](https://img.shields.io/badge/Memoria-128%20GB%20unificados-0071C5?style=flat-square)](#hardware)
[![OS](https://img.shields.io/badge/Fedora-44-51A2DA?style=flat-square&logo=fedora&logoColor=white)](#sistema)
[![Backend](https://img.shields.io/badge/backend-Vulkan%20%2F%20RADV-A41E22?style=flat-square&logo=vulkan&logoColor=white)](#por-qué-vulkan-y-no-rocm)
[![Licencia](https://img.shields.io/badge/licencia-MIT-green?style=flat-square)](LICENSE)

*Todo lo que hay aquí está **medido en la máquina**, no copiado de notas de prensa.*
*Cuando una medición desmiente a la anterior, se corrige y se deja constancia.*

</div>

---

## 🎯 De qué va esto

Un mini-PC de ~2.600 € con memoria unificada de 128 GB puede cargar modelos que no caben en ninguna GPU de consumo. La pregunta interesante no es *si* caben, sino **a qué velocidad real corren y qué configuración los hace ir más rápido**.

Este repositorio es el registro público de esas pruebas: cuantizaciones, tamaños de batch, ventanas de contexto, trampas del sistema operativo y conclusiones que resultaron ser falsas.

> **Hallazgo que resume el proyecto:** un MoE de **177B** parámetros genera al **doble de velocidad** que un modelo denso de **27B** en esta máquina. El cuello de botella no es el cómputo, es el ancho de banda de memoria — y eso lo cambia todo a la hora de elegir modelo.
>
> **Corolario, medido después:** ese mismo límite castiga a los MoE con *muchos* expertos activos. Un modelo de 313B que activa ~18B por token cae a 8,3 t/s — 3,3× más lento que el de 177B que activa ~6B: el triple de parámetros activos, el triple de lento. **Lo que importa no es el tamaño del modelo, son los parámetros activos.**
>
> *Corregido el 2026-10-17: este repo decía "~3B activos" para Flash-Next. La cabecera del GGUF dice otra cosa (10 expertos enrutados + 1 compartido de 512, más una tabla de n-gramas de ~51B que no cuenta como activa): son **~6B**. La ley sale reforzada. Ver [H-046](docs/hallazgos.md).*

---

## 📊 Resultados de un vistazo

Medido con `llama.cpp` sobre Vulkan/RADV. `pp` = prefill (leer el prompt), `tg` = generación (escribir la respuesta).

| Modelo | Params (activos) | Cuant. | Tamaño | pp t/s | tg t/s | Veredicto |
|---|---|---|---|---|---|---|
| **Qwen3.8-Flash-Next + MTP, motor Gufo 0.17.0** | 177B MoE (~6B) | UD-Q4_K_XL | 111 GB | **~1.400** ¶ | 25–67 ¶ | ⭐ En producción desde el 2026-10-17 (2 × 262k, visión) — H-049/H-051 |
| Qwen3.8-Flash-Next + MTP, llama.cpp | 177B MoE (~6B) | UD-IQ4_XS | 87 GiB | ~400 § | 28,7–45,9 § | Producción hasta el 2026-10-17 (MTP n-max 2, `-np 2`, 262k) |
| Qwen3.8-Flash-Next sin MTP | 177B MoE (~6B) | UD-IQ4_XS | 87 GiB | 345,0 ‡ | 22,2 ‡ | Configuración anterior |
| Qwen3.8-27B | 27B denso | Q4 | ~16 GiB | 365,1 | 13,1 | ❌ Inútil aquí |
| Qwen3-8B | 8B denso | Q4_K_M | 4,7 GiB | 1.286,8 | 45,4 | ✅ Referencia rápida |
| GLM-5.3-Flash | 313B MoE (~18B) | UD-IQ1_S | 93 GB | 124,8 | 8,3 | ⚠️ Correcto pero 3,3× lento |

‡ Medido contra `llama-server` con un prompt real de **24.782 tokens**, que es el caso de
uso de esta máquina. Las demás filas son de `llama-bench` con prompts cortos (512), donde
el KV cache apenas pesa: por eso su `tg` sale más alto. **No compares `tg` entre filas de
longitud distinta.** El mismo modelo da 27,7 t/s medido a 512 tokens.

§ Banco de **carga real** (`scripts/bench-real.py`): seis tareas con ficheros reales de
3.738 a 38.917 tokens (explicar y cambiar código, resumir prosa, extraer JSON), mediana de
dos pasadas tras calentamiento. 45,9 t/s es la extracción de JSON con ~5,6k tokens; 28,7 t/s,
el resumen de un documento de 38,9k. Kernel 7.2.9, build `81ef10ea5` + parche MTP
([H-043](docs/hallazgos.md)).

¶ Mismo banco de carga real, cifras del log de Gufo (H-049). En generación de un usuario empata con llama.cpp (mediana −2 %); la ventaja está en el prefill (×3,6), el primer turno de una conversación larga (×3,7) y la carga concurrente (87 t/s agregados frente a 38-44).

📄 **Detalle completo, metodología y datos crudos:** [`benchmarks/`](benchmarks/) · [`benchmarks/resultados.csv`](benchmarks/resultados.csv)

---

## 🆕 Lo último (septiembre–octubre 2026)

| | Hallazgo | En una línea |
|---|---|---|
| H-038 | **El MTP sí compensa** | La cabeza MTP como borrador especulativo da **+45,5 %** de generación (mediana) con carga real: código +48,7 %, prosa +37,2 %, JSON +69,6 %. El −40 % que medí antes era un artefacto del prompt sintético: sobre texto de relleno el borrador no acierta. |
| H-039 · H-041 | **Retadores descartados con medida** | Ornith-1.5-35B-A3B: +45 % de t/s pero un acierto menos (10/12 vs 11/12). Nemotron-3.5-Lightning-30B-A3B Q8: igual de lento (31,8 vs 32,4 t/s) y un acierto menos. Batería con verificador objetivo: el código se ejecuta, el JSON se parsea. |
| H-040 | **El modelo en reposo es gratis** | Con un segundo modelo cargado al lado, el principal no pierde nada (43,96 → 44,15 t/s). Generando los dos a la vez, cada uno cae ~30 % pero el agregado sube a 60,7 t/s. |
| H-043 | **Resultado nulo: el kernel** | Kernel 7.2.9 frente a 7.2.7: tg +0,00 %, pp −0,01 %. Actualizar el kernel es decisión de seguridad, no de rendimiento. |
| H-044 · H-045 · H-047 | **Gufo frente a llama.cpp** | **Prefill en frío ×4,5**: 42k tokens en 29 s frente a ~133 s (~1.440 frente a ~316 t/s). Además Gufo guarda puntos de control cada 10.240 tokens y reaprovecha un **prefijo parcial** (8,5-16 s cuando el prompt comparte la mitad); llama.cpp, con este modelo híbrido, solo reaprovecha el prompt idéntico. Con prompts cortos empatan. En calidad, la ventaja que parecía tener **no se sostiene**: el "fallo de contexto" de llama.cpp era el modelo **negándose** a dar un "código de autorización"; con una aguja neutra acierta 3/3 a 42k. Con greedy: llama.cpp 13/14, Gufo 14/14, con cuantizaciones distintas (Gufo 0.9.1 no carga la IQ4_XS: tensores IQ3_S). |
| H-046 | **Corrección: ~6B activos, no ~3B** | Leído de la cabecera del GGUF. Ver arriba. |
| H-048 | **El techo es la potencia, no el reloj** | Fijar el reloj de la GPU da +2,3 % (no el ~20 % que se ve en otras máquinas): el SoC está clavado en **85 W** y el reloj sostenido apenas sube. Cuesta +7,5 W en reposo, 24/7. Cargar sin mmap: 0 %. No se adopta ninguno. |
| H-049 | **Gufo 0.17.0, apto para producción** | Con 2 sesiones × 262k, visión y MTP: prefill **×3,6**, generación de un usuario **empatada**, **+57 %** agregado con peticiones simultáneas, 0 fugas entre sesiones, salida determinista en paralelo, tool calling y visión OK, calidad 14/14. Exige `mmproj` BF16 y la Q4_K_XL. |
| H-050 | **Afinidad de sesión: no hace falta** | Tres conversaciones de ~15k intercaladas contra 2 slots: llama.cpp no reprocesa nada (su caché en RAM restaura cada conversación; solo procesa los 24-30 tokens nuevos). Lo que cuesta es el primer turno, y ahí Gufo es **×3,7** (11 s frente a 42 s). Gufo solo admite **una** clave de API (H-050b). |

---

## 🧠 Los siete hallazgos que más ahorran tiempo

### 0. Una sola flag daba +92% de prefill en el banco — +16% en el servidor real
`llama.cpp` trae la *carga diferida* de tensores en `auto` por defecto, y en iGPU sale
carísima. `llama-bench` mide **216 → 415 t/s** (+92%) con solo añadir `--lazy-mode off`;
contra el servidor real, con un prompt de 24k, la ganancia es de **+16%** — real y gratis,
pero un tercio de lo que promete el banco sintético. El fix que lo desactiva entró diez
horas después del commit con el que estaba compilado el binario de producción.

Cómo detectarlo sin leer changelogs: compara tu rendimiento con el techo de ancho de banda
— los densos rendían al 78–87% de su techo, el MoE al **19%**. Esa asimetría es la señal.
Y **cuidado**: desactivarla hace los pesos no reclamables, lo que tumbó `ubatch 4096`.
👉 [`docs/carga-diferida-y-oom.md`](docs/carga-diferida-y-oom.md)

### 1. El modelo denso mediano no tiene sitio en esta máquina
27B denso a 13 t/s vs 177B MoE a 27 t/s. Con memoria unificada lenta comparada con VRAM (~256 GB/s frente a ~1 TB/s de una GPU dedicada), lo que manda es **cuántos bytes de pesos hay que leer por token**. Un MoE que activa ~6B lee muchísimo menos que un denso que activa 27B, aunque pese cinco veces más en disco. **Regla práctica: en Strix Halo, MoE grande > denso mediano.**

### 2. …pero un MoE con muchos expertos activos vuelve a ser lento
El corolario del punto anterior, comprobado a la mala: un MoE de **313B que activa ~18B** por token rinde **8,3 t/s**, frente a los **27,4 t/s** del MoE de 177B que activa ~6B. Pesa lo mismo en memoria (93 vs 87 GB) y da respuestas correctas, pero va **3,3× más despacio** con el triple de parámetros activos. La cifra que predice el rendimiento es **parámetros activos**, no parámetros totales ni gigabytes en disco.
👉 [`benchmarks/glm53-flash.md`](benchmarks/glm53-flash.md)

### 3. `llama-bench` con *otro* modelo te miente
El barrido de `ubatch` hecho con un modelo pequeño daba una curva **descendente** y recomendaba `ub 1024`. Midiendo contra `llama-server` con el modelo real y un prompt real de 33k tokens, la curva
es **ascendente**. Son conclusiones opuestas.

**Corrección posterior, ya cerrada con medidas:** publiqué `ub 4096` como recomendación y
era un error doble. Rehecho el barrido con `--lazy-mode off`, el mejor punto medido es
**2048** (345,0 t/s) y **`ubatch 4096` ni siquiera arranca**: muere por OOM al cargar el
modelo y systemd entra en bucle de reintentos. El salto de 1024 a 2048 son solo **+2,8%**,
un margen medido con **2 pasadas por punto**: suficiente para descartar 4096 (no arranca),
pero **no** para dar 2048 como óptimo firme frente a 1024. Pendiente de rehacer con
calentamiento separado, 5 pasadas y orden equilibrado; hasta entonces la diferencia
1024↔2048 queda como *no concluyente*.
👉 [`docs/hallazgos.md`](docs/hallazgos.md) H-011
👉 [`docs/metodologia.md`](docs/metodologia.md) — cómo medir sin engañarse.

### 4. El carveout de VRAM en BIOS es irrelevante (con Vulkan)
Subir el *UMA Frame Buffer* de la BIOS no aporta **nada**: RADV suma VRAM + GTT en un único pool. Medido con el mismo modelo antes y después: pp 1.286,8 vs 1.279,2 · tg 45,4 vs 45,3 — ruido. **Deja el carveout al mínimo** y regula la memoria por parámetros del kernel.
👉 [`docs/bios-y-memoria.md`](docs/bios-y-memoria.md)

### 5. SELinux tira abajo el servicio y no te dice por qué
Un binario compilado fuera de `/usr` arranca a mano pero falla como servicio systemd con `203/EXEC: Permission denied`. No es un permiso de fichero, es la etiqueta de SELinux.
👉 [`docs/servicio-systemd.md`](docs/servicio-systemd.md) — el `semanage fcontext` que lo arregla.

### 6. Un contexto de 256k no cuesta 256k de KV cache
El modelo en producción declara ventana nativa de **262.144 tokens** y **reserva** esa
ventana entera por slot, con dos slots simultáneos (verificado en el log). Aviso honesto:
lo que está *medido* llega solo hasta **33k tokens** — la cifra de 256k es capacidad
reservada, no validada de punta a punta. Es viable porque de sus 48 capas solo 12 son de atención; el resto son capas lineales de estado fijo. La caché KV apenas crece con el contexto.
👉 [`docs/ventana-de-contexto.md`](docs/ventana-de-contexto.md)

---

## 🖥️ Hardware

| | |
|---|---|
| **CPU** | AMD Ryzen AI MAX+ 395 — 16 núcleos / 32 hilos (Zen 5) |
| **iGPU** | Radeon 8060S — 40 CU RDNA 3.5, `gfx1151` |
| **Memoria** | 128 GB LPDDR5X unificados (CPU e iGPU comparten el pool) |
| **Almacenamiento** | NVMe 2 TB + segunda ranura M.2 libre |
| **Formato** | Mini-PC de sobremesa, refrigeración activa |

<a id="sistema"></a>
### Sistema

| | |
|---|---|
| **SO** | Fedora Server 44 |
| **Kernel** | 7.2.10-200.fc44 (7.2.7 → 7.2.9 medido sin diferencia, H-043) |
| **Driver gráfico** | Mesa 26.2.3 · RADV |
| **Runtime** | Gufo 0.17.0 (contenedor rootless, ROCm/HIP) desde el 2026-10-17; antes `llama.cpp` `81ef10ea5` + parche MTP sobre Vulkan |
| **Arranque** | `amd_iommu=off` · `amdgpu.gttsize=126976` · `ttm.pages_limit=32505856` |
| **Perfil** | `tuned accelerator-performance` |

Con esa configuración el pool visible para inferencia es de **~124 GB**.

### ¿Por qué Vulkan y no ROCm?

En `gfx1151` el backend Vulkan/RADV es hoy más rápido y muchísimo más estable que ROCm, que en varias versiones ha dado desde regresiones de rendimiento hasta **logits incorrectos sin llegar a fallar** (el peor tipo de bug: responde, pero mal). Las cifras de llama.cpp de este repo son todas Vulkan.

**Excepción, desde H-049/H-051:** Gufo, el motor de producción actual, va sobre ROCm/HIP con kernels propios para `gfx1151`. La advertencia de arriba es sobre llama.cpp con ROCm; a Gufo se le exigió la misma batería de calidad verificable antes de adoptarlo (14/14, salida determinista).
👉 [`docs/instalacion.md`](docs/instalacion.md)

---

## 📁 Estructura

```
├── README.md                    ← estás aquí
├── docs/
│   ├── instalacion.md           Fedora 44, kernel, Mesa, compilar llama.cpp
│   ├── bios-y-memoria.md        UMA carveout, GTT, el experimento que salió en nada
│   ├── servicio-systemd.md      Servir el modelo 24/7 (+ la trampa de SELinux)
│   ├── metodologia.md           Cómo medimos y por qué así
│   ├── ventana-de-contexto.md   256k servidos, 98k verificados con aguja
│   ├── carga-diferida-y-oom.md  La flag que duplicó el prefill y el OOM que mató la máquina
│   ├── forks-evaluacion.md      Forks y motores alternativos evaluados
│   ├── hallazgos.md             Bitácora de conclusiones (incluidas las erróneas)
│   └── plan-de-pruebas.md       Hoja de ruta: texto → imagen → vídeo
├── benchmarks/
│   ├── resultados.csv           Datos crudos, una fila por medición
│   ├── qwen38-flash-next.md     Barrido completo de ubatch
│   ├── plan-de-pruebas.md       Hoja de ruta del laboratorio
│   ├── glm53-flash.md           313B a 1 bit: cabe, acierta, pero va lento
│   └── comparativa-modelos.md   MoE vs denso, y activos vs totales
└── scripts/
    ├── bench-ubatch.py          Mide pp/tg contra llama-server con prompt real
    ├── bench-context.py         Barrido de longitud de contexto + prueba de aguja
    ├── bench-real.py            Banco de carga real: seis tareas con ficheros reales (H-038, H-043)
    ├── bench-calidad-motores.py 12 pruebas con verificador objetivo entre dos servidores (H-044)
    ├── h045-celdas-dudosas.py   Repite las celdas dudosas de H-044 con muestreo greedy y N pasadas
    ├── modelos/                 Batería de 14 prompts y verificador de H-039/H-041, coexistencia H-040
    ├── bench-calidad.py         Lanza la batería de calidad y guarda respuestas crudas
    ├── verifica-codigo.py       Compila y ejecuta el código que genera el modelo
    ├── campana.py               Runner de campañas A/B: gates, rollback y fases enchufables
    ├── banco.py                 Servidor de banco: ExecStart real, clave fuera de argv, y muere siempre
    ├── fases_h032.py            H-032: corrección de la caché de prompt, regresión #28495, TTFT
    ├── fases_h033.py            H-033: A/B de builds alternado + igualdad greedy
    ├── fases_h034.py            H-034: cabeza MTP sidecar (np=1), escalera de contexto y visión
    ├── fases_h035.py            H-035: MTP en la línea productiva (np=2 -kvu): diagnóstico greedy con logprobs, aislamiento, velocidad, visión; despliega
    ├── fases_h036.py            H-036: cambios de drluoto uno a uno (nmax3, frspec, requant, kvzero) contra producción
    ├── builds.sh                Builds por SHA en /models/llama-builds + symlink de promoción
    ├── prompts.py               Corpus medido con el tokenizador real y congelado con su sha256
    ├── salud.py                 La única espera: unidad activa + /health + modelo correcto
    ├── validacion.py            Contratos de respuesta, un contrato por tipo de prueba
    ├── credencial.sh            Deriva la clave de API del ExecStart efectivo de la unidad
    ├── smoke-test.sh            Comprobación rápida de carga y coherencia (17×23 exacto)
    └── restauracion.sh          ¿Quedó producción como estaba tras un barrido?
```

---

## 🗺️ Plan de pruebas

La hoja de ruta completa (fases: texto → imagen → vídeo, con criterios de decisión) vive en
[`docs/plan-de-pruebas.md`](docs/plan-de-pruebas.md).

## 🚧 En curso

- [x] **GLM-5.3-Flash UD-IQ1_S (93 GB)** — cargado y medido: coherente a ~1 bit/peso, pero 8,3 t/s. No sustituye al modelo en producción. → [`benchmarks/glm53-flash.md`](benchmarks/glm53-flash.md)
- [x] Variante **GLM-5.3-Flash-REAP50-IQ4_XS** (50 % de expertos podados): arranca con un build parcheado, pero no compensa. → [`docs/hallazgos.md`](docs/hallazgos.md) H-018
- [x] **Contexto largo verificado hasta 98k tokens** con la config de producción: generación −50% (26,3 → 13,0 t/s) por el KV cache, prefill −32%, y **aguja recuperada 6/6** con el dato enterrado a la mitad del texto. ~100k tokens ≈ 8 min de prefill. → [`docs/hallazgos.md`](docs/hallazgos.md) H-012
- [ ] Empujar la verificación de ventana hasta 131k reales y 262k de punta a punta.
- [x] **Qwen3-Next-80B-A3B** (IQ4_XS, 42,6 GB): más rápido (~3B activos frente a ~6B, coherente con la ley), pero 15,5/20 frente a 18,5/20 en calidad, sobre todo en castellano/euskera. → H-016
- [x] Repetir las celdas dudosas de H-044 con muestreo igualado y 3 pasadas → H-045
- [x] Aguja neutra y mismo GGUF → H-047 (la negativa queda confirmada; Gufo 0.9.1 no carga IQ3_S, así que el mismo GGUF no es posible)
- [x] Gufo con el contexto de producción: cabe, sin fugas, visión y herramientas OK, decode con el banco real → H-049
- [x] Reloj de GPU fijo y carga sin mmap → H-048 (no compensan)
- [x] Conversaciones largas intercaladas contra 2 slots (patrón de Hermes) → H-050 (la afinidad no hace falta)
- [x] **Gufo en producción** con el drop-in [`scripts/60-gufo.conf`](scripts/60-gufo.conf) (mismo nombre de unidad, una sola clave, caché RAM 4 GiB) → H-051
- [ ] Repetir GLM cuando el soporte `glm5next` entre en upstream y Vulkan implemente las operaciones fusionadas que hoy se desactivan.

---

## ⚠️ Cómo leer estos números

- Todas las medidas son **de esta máquina concreta**, con esta versión de Mesa y de `llama.cpp`. Cambia cualquiera de las dos y pueden moverse.
- `pp`/`tg` se miden con **prompts reales contra el servidor**, no con benchmarks sintéticos de otro modelo. Ver hallazgo nº 2.
- Cada punto son varias pasadas con reinicio del servicio entre configuraciones, para no arrastrar caché.
- Si una cifra publicada aquí resulta ser incorrecta, se **corrige y se marca como corregida**, no se borra.

---

<div align="center">

**Licencia MIT** · Cuaderno de laboratorio personal, sin afiliación con AMD ni con los autores de los modelos.

</div>
