# H-052 — DeepSeek-V4.1-Flash (763B) en el Strix Halo por Strata

**Fecha:** 2026-10-25 · **Estado:** logrado (prueba de concepto completa, no es producción)

El motor DeepSeek de [Strata](https://github.com/Niko1221/Strata) (`strata-dsv41`) se
compila **desde fuente para `gfx1151`** y corre el modelo
`DeepSeek-V4.1-Flash-1017` (763B, MoE de 384 expertos / 6 activos) en el M5 con
velocidades de multi-GPU profesional. El repo publica binarios para CUDA y
`hip-rx9070`, pero **no para Strix Halo**: hay que compilarlo a mano. Todo lo
necesario queda en `/models/strata-lab/` del M5.

## 1. Qué se hizo

1. ROCm 7.14.1 TheRock para gfx1151 (`repo.amd.com/.../therock-dist-linux-gfx1151-7.14.1.tar.gz`,
   1,7 GB, SHA-256 `c40e8f2b…ca24afb00`) extraído en `/models/strata-lab/rocm/install`.
2. `git clone` de Strata (`41cf05a`) y compilación:

```sh
cmake -S . -B build-halo -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_HIP=ON -DSTRATA_ENABLE_CUDA=OFF -DSTRATA_PREFILL_MMQ=ON \
  -DSTRATA_BUILD_DSV41=ON -DCMAKE_HIP_ARCHITECTURES=gfx1151 \
  -DCMAKE_HIP_COMPILER=$SDK/lib/llvm/bin/clang++ -DCMAKE_HIP_COMPILER_ROCM_ROOT=$SDK \
  "-DCMAKE_PREFIX_PATH=$SDK;$SDK/lib/rocm_sysdeps;$SDK/lib/llvm" \
  "-DCMAKE_HIP_FLAGS=--rocm-path=$SDK --rocm-device-lib-path=$SDK/lib/llvm/amdgcn/bitcode"
cmake --build build-halo --target strata strata-dsv41 -j 4
```

   Resultado: `strata-dsv41 0.1.50 build 41cf05a06` (17,9 MB) y `strata` (37,5 MB).

3. Descarga del modelo `IST-DAS-2.00bpw` (257 GB: 138 GB modelo + 111 GB tabla
   n-gram Engram + 8 GB drafter DSpark) con `setup.py --family deepseek41`.
   El `--check` reconoce el M5: **14.258 de 15.360 expertos caben en memoria gráfica**.
   El smoke test del setup responde con sentido.

## 2. La trampa del arranque (y su solución)

Con la config por defecto el motor **se atasca para siempre** en Strix Halo:
`still starting` 25+ min, GTT a 125 GB, lectura de disco congelada, un hilo al
93 % de CPU y GPU al 2 %. Es el mismo patrón de las issues
[#4033](https://github.com/Niko1221/Strata/issues/4033),
[#4116](https://github.com/Niko1221/Strata/issues/4116) y
[#4117](https://github.com/Niko1221/Strata/issues/4117) (todas del 24-25 oct,
Ryzen AI Max+ 395).

`--mmap-experts on` **no basta** (se atasca igual). La bandera que lo cura es
**`--vram-experts F`**: la fracción del presupuesto de VRAM que se reserva para
expertos, que vale **1.0 por defecto** — en memoria unificada eso intenta
residenciar ~123 GB de expertos en una máquina de 124 GB.

```sh
# la config de arranque que funciona (strata-dsv41, no el motor Qwen):
--pack .../deepseek-v4.1-flash-ist-das-2.00bpw
--max-context 131072 --eos 1 --dspark auto --dspark-conf 0.65
--mmap-experts on --vram-experts 0.5
```

**Ojo:** `--resident-experts` (la que propone #4117) **NO existe en el motor
DeepSeek**, es del motor Qwen (`strata`). Cada motor del repo tiene su propio
juego de flags; compruébese el `--help` del binario concreto.

Con `--vram-experts 0.5` arranca en ~2 min y el log imprime el reparto sano:
6.200 de 15.744 expertos en VRAM (57,7 GB), 9.550 en la vía CPU leídos en sitio,
los 3.772 más usados precargados en RAM en 6 s.

## 3. Rendimiento medido (M5, IST-DAS 2.00bpw)

| escenario | generación | prefill | DSpark |
|---|---:|---:|---:|
| código (161 tok) | **24,3 t/s** | — | 102/119 (86 %) |
| prosa 700 tok | **20,2 t/s** | 28,8 t/s | 277/343 (81 %) |
| aguja a 105.474 tok | 12,8–26,8 t/s | **213–216 t/s** | — |

Referencias que publica el proyecto: RTX 5070 12 GB ≈ 4,7 t/s, RTX 3060 12 GB ≈
7,2-10 t/s, cuatro Radeon AI PRO R9700 ≈ 31-48 t/s. **El M5 se sitúa en el rango
del multi-GPU profesional** (3-5× cualquier PC de gaming) para este modelo.

Aun así: sigue por debajo de Qwen3.8-Flash-Next en la misma máquina (25-49 t/s),
el motor DeepSeek **atiende una petición a la vez**, y el prefill a 100k va 8× más
rápido que con prompt corto porque el paso por trozos necesita volumen.

## 4. Baterías de aptitud

**Calidad** (`scripts/bench-calidad-motores.py`, 12 pruebas con verificador objetivo):

| config | resultado | notas |
|---|---|---|
| ctx 8192 | 8/12 | las 2 agujas mueren con 400 (prompt 46.574 tok) |
| ctx 65536 | **10/12** | las 2 agujas (~46k tok) aciertan: 228 s y 220 s |

Los 2 fallos restantes son idénticos en ambas configuraciones:
- `idioma-castellano`: **falso negativo del verificador** — la respuesta es
  castellano impecable pero corta, con solo 2 de las 3 marcas (" el ", " la "…).
- `razonamiento-trampa`: fallo real — responde 100 a las cien máquinas.

**Aptitud de producción** (`scripts/h049-pruebas.py`, `--solo fugas,herramientas`):
- fugas entre 8 conversaciones (serie y paralelo): **0**
- tool calling: **OK** (`get_weather` → `city=Bilbao`)
- concurrencia y visión: no aplican — el motor las ignora con aviso.

## 5. Contexto útil

8k → 64k → **128k arrancan**. A 105.474 tokens reales las dos agujas (50 % y 90 %
de profundidad) **aciertan**. No es contexto reservado, es útil.

## 6. Archivos

- Evidencias crudas: `evidencias/h052-deepseek-20261025/` (`cal-deepseek.jsonl`,
  `cal-deepseek-64k.jsonl`, `h049-deepseek.json`, `aguja100k.json`).
- En el M5: `/models/strata-lab/{rocm,strata,models,data,venv-strata}/` y las
  configs `cfg-ctx128k.json` (la buena) en `/models/strata-lab/strata/`.

## 7. Arranque para repetir

```sh
# en el M5, con llama-flashnext parado (el centinela /etc/llama-guard.disable puesto)
cd /models/strata-lab/strata
SDK=/models/strata-lab/rocm/install
env STRATA_HIPBLASLT_TUNING=$PWD/tools/hip/gfx1151-hipblaslt-100401.txt \
  LD_LIBRARY_PATH=$SDK/lib:$SDK/lib/rocm_sysdeps/lib:$SDK/lib/llvm/lib \
  /models/strata-lab/venv-strata/bin/python serve/server.py \
  --engine strata --config cfg-ctx128k.json --port 8085
# OpenAI-compatible en http://127.0.0.1:8085/v1 (cualquier clave; modelo deepseek-v4.1-flash)
```
