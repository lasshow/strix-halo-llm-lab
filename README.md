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
> **Corolario, medido después:** ese mismo límite castiga a los MoE con *muchos* expertos activos. Un modelo de 313B que activa 18B por token cae a 8,3 t/s — 3,3× más lento que el de 177B que activa 3B. **Lo que importa no es el tamaño del modelo, son los parámetros activos.**

---

## 📊 Resultados de un vistazo

Medido con `llama.cpp` sobre Vulkan/RADV. `pp` = prefill (leer el prompt), `tg` = generación (escribir la respuesta).

| Modelo | Params (activos) | Cuant. | Tamaño | pp t/s | tg t/s | Veredicto |
|---|---|---|---|---|---|---|
| **Qwen3.8-Flash-Next** | 177B MoE (~3B) | UD-IQ4_XS | 87 GiB | **345,0** ‡ | **22,2** ‡ | ⭐ En producción (`--lazy-mode off`, `ub 2048`) |
| Qwen3.8-27B | 27B denso | Q4 | ~16 GiB | 365,1 | 13,1 | ❌ Inútil aquí |
| Qwen3-8B | 8B denso | Q4_K_M | 4,7 GiB | 1.286,8 | 45,4 | ✅ Referencia rápida |
| GLM-5.3-Flash | 313B MoE (~18B) | UD-IQ1_S | 93 GB | 124,8 | 8,3 | ⚠️ Correcto pero 3,3× lento |

‡ Medido contra `llama-server` con un prompt real de **24.782 tokens**, que es el caso de
uso de esta máquina. Las demás filas son de `llama-bench` con prompts cortos (512), donde
el KV cache apenas pesa: por eso su `tg` sale más alto. **No compares `tg` entre filas de
longitud distinta.** El mismo modelo da 27,7 t/s medido a 512 tokens.

📄 **Detalle completo, metodología y datos crudos:** [`benchmarks/`](benchmarks/) · [`benchmarks/resultados.csv`](benchmarks/resultados.csv)

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
27B denso a 13 t/s vs 177B MoE a 27 t/s. Con memoria unificada lenta comparada con VRAM (~256 GB/s frente a ~1 TB/s de una GPU dedicada), lo que manda es **cuántos bytes de pesos hay que leer por token**. Un MoE que activa 3B lee muchísimo menos que un denso que activa 27B, aunque pese cinco veces más en disco. **Regla práctica: en Strix Halo, MoE grande > denso mediano.**

### 2. …pero un MoE con muchos expertos activos vuelve a ser lento
El corolario del punto anterior, comprobado a la mala: un MoE de **313B que activa ~18B** por token rinde **8,3 t/s**, frente a los **27,4 t/s** del MoE de 177B que activa ~3B. Pesa lo mismo en memoria (93 vs 87 GB) y da respuestas correctas, pero va **3,3× más despacio**. La cifra que predice el rendimiento es **parámetros activos**, no parámetros totales ni gigabytes en disco.
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
| **Kernel** | 7.2.4-200.fc44 |
| **Driver gráfico** | Mesa 26.1.8 · RADV · Vulkan 1.4.354 |
| **Runtime** | `llama.cpp` compilado con `-DGGML_VULKAN=ON` |
| **Arranque** | `amd_iommu=off` · `amdgpu.gttsize=126976` · `ttm.pages_limit=32505856` |
| **Perfil** | `tuned accelerator-performance` |

Con esa configuración el pool visible para inferencia es de **~124 GB**.

### ¿Por qué Vulkan y no ROCm?

En `gfx1151` el backend Vulkan/RADV es hoy más rápido y muchísimo más estable que ROCm, que en varias versiones ha dado desde regresiones de rendimiento hasta **logits incorrectos sin llegar a fallar** (el peor tipo de bug: responde, pero mal). Todas las cifras de este repo son Vulkan.
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
    ├── bench-calidad.py         Lanza la batería de calidad y guarda respuestas crudas
    ├── verifica-codigo.py       Compila y ejecuta el código que genera el modelo
    ├── campana.py               Runner de campañas A/B: gates, rollback y fases enchufables
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
- [ ] Probar la variante **REAP50-IQ4_XS (88 GB)**: mismo modelo con el 50% de expertos podados y cuantización decente. Hipótesis: menos expertos activos ⇒ más rápido, y mejor precisión por peso.
- [x] **Contexto largo verificado hasta 98k tokens** con la config de producción: generación −50% (26,3 → 13,0 t/s) por el KV cache, prefill −32%, y **aguja recuperada 6/6** con el dato enterrado a la mitad del texto. ~100k tokens ≈ 8 min de prefill. → [`docs/hallazgos.md`](docs/hallazgos.md) H-012
- [ ] Empujar la verificación de ventana hasta 131k reales y 262k de punta a punta.
- [ ] **Qwen3-Next-80B-A3B** (IQ4_XS, 42,6 GB, ya descargado): mismos ~3B activos que Flash-Next en la mitad de memoria. ¿Cuánta calidad compra el doble de expertos totales a igual velocidad teórica?
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
