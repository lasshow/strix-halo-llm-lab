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

Un mini-PC de ~1.500 € con memoria unificada de 128 GB puede cargar modelos que no caben en ninguna GPU de consumo. La pregunta interesante no es *si* caben, sino **a qué velocidad real corren y qué configuración los hace ir más rápido**.

Este repositorio es el registro público de esas pruebas: cuantizaciones, tamaños de batch, ventanas de contexto, trampas del sistema operativo y conclusiones que resultaron ser falsas.

> **Hallazgo que resume el proyecto:** un MoE de **177B** parámetros genera al **doble de velocidad** que un modelo denso de **27B** en esta máquina. El cuello de botella no es el cómputo, es el ancho de banda de memoria — y eso lo cambia todo a la hora de elegir modelo.

---

## 📊 Resultados de un vistazo

Medido con `llama.cpp` sobre Vulkan/RADV. `pp` = prefill (leer el prompt), `tg` = generación (escribir la respuesta).

| Modelo | Params (activos) | Cuant. | Tamaño | pp t/s | tg t/s | Veredicto |
|---|---|---|---|---|---|---|
| **Qwen3.8-Flash-Next** | 177B MoE (~3B) | UD-IQ4_XS | 87 GiB | **299,8** | **27,4** | ⭐ En producción |
| Qwen3.8-27B | 27B denso | Q4 | ~16 GiB | 365,1 | 13,1 | ❌ Inútil aquí |
| Qwen3-8B | 8B denso | Q4_K_M | 4,7 GiB | 1.286,8 | 45,4 | ✅ Referencia rápida |
| GLM-5.3-Flash | ~250B MoE (~18B) | UD-IQ1_S | 93 GB | ⏳ | ⏳ | 🚧 En pruebas |

📄 **Detalle completo, metodología y datos crudos:** [`benchmarks/`](benchmarks/) · [`benchmarks/resultados.csv`](benchmarks/resultados.csv)

---

## 🧠 Los cinco hallazgos que más ahorran tiempo

### 1. El modelo denso mediano no tiene sitio en esta máquina
27B denso a 13 t/s vs 177B MoE a 27 t/s. Con memoria unificada lenta comparada con VRAM (~256 GB/s frente a ~1 TB/s de una GPU dedicada), lo que manda es **cuántos bytes de pesos hay que leer por token**. Un MoE que activa 3B lee muchísimo menos que un denso que activa 27B, aunque pese cinco veces más en disco. **Regla práctica: en Strix Halo, MoE grande > denso mediano.**

### 2. `llama-bench` con *otro* modelo te miente
El barrido de `ubatch` hecho con un modelo pequeño daba una curva **descendente** y recomendaba `ub 1024`. Midiendo contra `llama-server` con el modelo real y un prompt real de 33k tokens, la curva es **ascendente** y el ganador es `ub 4096`. Son conclusiones opuestas.
👉 [`docs/metodologia.md`](docs/metodologia.md) — cómo medir sin engañarse.

### 3. El carveout de VRAM en BIOS es irrelevante (con Vulkan)
Subir el *UMA Frame Buffer* de la BIOS no aporta **nada**: RADV suma VRAM + GTT en un único pool. Medido con el mismo modelo antes y después: pp 1.286,8 vs 1.279,2 · tg 45,4 vs 45,3 — ruido. **Deja el carveout al mínimo** y regula la memoria por parámetros del kernel.
👉 [`docs/bios-y-memoria.md`](docs/bios-y-memoria.md)

### 4. SELinux tira abajo el servicio y no te dice por qué
Un binario compilado fuera de `/usr` arranca a mano pero falla como servicio systemd con `203/EXEC: Permission denied`. No es un permiso de fichero, es la etiqueta de SELinux.
👉 [`docs/servicio-systemd.md`](docs/servicio-systemd.md) — el `semanage fcontext` que lo arregla.

### 5. Un contexto de 256k no cuesta 256k de KV cache
El modelo en producción declara ventana nativa de **262.144 tokens** y la sirve entera **por slot**, con dos slots simultáneos. Es viable porque de sus 48 capas solo 12 son de atención; el resto son capas lineales de estado fijo. La caché KV apenas crece con el contexto.
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
| **Kernel** | 7.1.13 |
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
│   ├── ventana-de-contexto.md   256k reales: qué lo hace posible
│   └── hallazgos.md             Bitácora de conclusiones (incluidas las erróneas)
├── benchmarks/
│   ├── resultados.csv           Datos crudos, una fila por medición
│   ├── qwen38-flash-next.md     Barrido completo de ubatch
│   └── comparativa-modelos.md   MoE vs denso
└── scripts/
    ├── bench-ubatch.py          Mide pp/tg contra llama-server con prompt real
    └── smoke-test.sh            Comprobación rápida de carga y coherencia
```

---

## 🚧 En curso

- [ ] **GLM-5.3-Flash UD-IQ1_S (93 GB)** — descargado, compilado el soporte (`glm5next`, aún sin mergear en upstream). Pendiente: primera carga y medición.
- [ ] Verificar estabilidad a contexto largo (>90k) con `ubatch` alto.
- [ ] Plan B de calidad: variante con expertos podados y cuantización menos agresiva.

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
