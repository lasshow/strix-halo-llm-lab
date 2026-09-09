# Instalación y compilación

## Sistema base

**Fedora Server 44**, kernel 7.1.x. La elección no es arbitraria: Fedora 43 rompió ROCm en `gfx1151` y los contenedores comunitarios de referencia para Strix Halo se construyen sobre fc44.

## Parámetros de arranque del kernel

```
amd_iommu=off amdgpu.gttsize=126976 ttm.pages_limit=32505856
```

- `amd_iommu=off` — entre 5% y 12% más de rendimiento. La traducción de direcciones del IOMMU penaliza las transferencias masivas hacia la iGPU.
- `amdgpu.gttsize=126976` — GTT en MB (~124 GB). **No está deprecado**, pese a lo que se lee por ahí.
- `ttm.pages_limit=32505856` — hay que ponerlo **junto** al anterior; sin él el límite real lo marca TTM y `gttsize` no sirve de nada.

Perfil de energía: `tuned-adm profile accelerator-performance`.

## BIOS

| Opción | Valor |
|---|---|
| iGPU Configuration | `UMA_SPECIFIED` (en `Auto` no aparece el tamaño) |
| UMA Frame Buffer Size | **el mínimo disponible** |
| Power Mode Select | `Performance` |
| Secure Boot | Disabled |

El carveout grande no aporta nada — ver [`bios-y-memoria.md`](bios-y-memoria.md).

## Comprobar que la memoria está bien

```bash
free -g                        # MemTotal debe reflejar casi toda la RAM
vulkaninfo --summary | grep -E "deviceName|driverName|driverInfo"
```

Debe salir `AMD Radeon 8060S Graphics (RADV STRIX_HALO)` con `radv` / Mesa 26.x. Si aparece solo `llvmpipe`, estás en software y no hay aceleración.

## Compilar llama.cpp con Vulkan

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -G Ninja \
  -DGGML_VULKAN=ON \
  -DGGML_HIP_ROCWMMA_FATTN=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_CURL=OFF
cmake --build build -j $(( $(nproc) - 4 ))
```

Compilación completa (~860 objetivos): unos 20 minutos con 28 hilos.

## Por qué Vulkan y no ROCm

En `gfx1151`, a día de hoy:

- **Vulkan/RADV** funciona sin sobresaltos y es competitivo o más rápido.
- **ROCm** ha ido acumulando problemas en esta iGPU, incluido un fallo que devolvía **logits incorrectos sin llegar a fallar**: el modelo responde, pero mal. Es el peor tipo de bug posible, porque no lo detectas mirando si arranca.

Si compilas con HIP para experimentar, valida siempre la **corrección** (respuestas coherentes, perplejidad razonable) **antes** de mirar los tokens/s. Un backend roto puede parecer rapidísimo.

## Compilar soporte experimental de un modelo nuevo

Cuando una arquitectura aún no está mergeada en upstream, se trabaja en un clon **separado** para no tocar la instalación que sirve en producción:

```bash
git clone https://github.com/ggml-org/llama.cpp llama-experimental
cd llama-experimental
git fetch https://github.com/<autor>/llama.cpp <rama>:local-exp
git checkout local-exp
grep -r "LLM_ARCH_<NOMBRE>" src/llama-arch.h   # confirmar que el soporte está ahí
```

Ojo con los nombres: buscar el token exacto (p. ej. `glm5next`, no `glm5_next`) o tendrás un falso negativo.
