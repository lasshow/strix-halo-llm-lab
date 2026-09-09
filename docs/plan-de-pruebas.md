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
