# GLM-5.3-Flash — 313B a 1 bit por peso

**Fecha:** 2026-09-09 · **Cuantización:** `UD-IQ1_S` (Unsloth Dynamic) · **Tamaño:** 93,1 GB
**Arquitectura:** `glm5next` — MoE de 313B parámetros totales, **~18B activos** por token
**Build:** `llama.cpp` con el PR de soporte `glm5next`, **aún sin mergear en upstream**

## Pregunta que queríamos responder

¿Merece la pena un modelo de 313B comprimido a ~1 bit por peso, frente a uno de 177B con
cuantización holgada (IQ4_XS)? Los dos ocupan prácticamente lo mismo en memoria —
93 GB contra 87 GiB — así que la máquina obliga a elegir uno de los dos.

Había dos dudas razonables:

1. **¿Sobrevive a 1 bit?** A esa compresión la degradación suele ser severa.
2. **¿A qué velocidad va?** Activa 6× más parámetros por token que el modelo en producción.

## Resultado corto

Sobrevive bien. Va lento.

| Métrica | GLM-5.3-Flash IQ1_S | Qwen3.8-Flash-Next IQ4_XS | Diferencia |
|---|---|---|---|
| Parámetros totales | 313B | 177B | +77% |
| **Parámetros activos** | **~18B** | **~3B** | **6×** |
| Tamaño en disco | 93,1 GB | 87 GiB | ≈ igual |
| Prefill (`pp`) | 124,8 t/s | 299,8 t/s † | **2,4× más lento** |
| Generación (`tg`) | **8,3 t/s** | **27,4 t/s** | **3,3× más lento** |
| Carga del modelo | ~20 s | ~20 s | igual |

## Calidad: 5 de 5

Cinco pruebas, `temperature 0`, presupuesto de tokens amplio:

| Prueba | Pedido | Respuesta | ¿Bien? |
|---|---|---|---|
| Aritmética | `17*23`, solo el número | `391` | ✅ |
| Seguir instrucciones | Tres palabras con M, separadas por comas | `Manzana, Montaña, Música` | ✅ |
| Razonamiento | Tren sale 14:00, tarda 2h45m | `16:45` | ✅ |
| Código | Función Python que invierta una cadena | `def invertir_cadena(cadena): return cadena[::-1]` | ✅ |
| Español | Memoria unificada en dos frases | Explicación correcta y natural | ✅ |

**No se ha roto a 1 bit por peso.** Las cuantizaciones dinámicas de Unsloth (que asignan
más bits a las capas sensibles) hacen su trabajo. Esto era lo que más dudas generaba
y la respuesta es claramente positiva.

## El coste oculto: razona muchísimo

Los t/s no cuentan toda la historia. El modelo genera cadenas de razonamiento largas
antes de responder, y eso multiplica la latencia percibida:

| Prueba | Razonamiento | Tokens totales | Tiempo hasta la respuesta |
|---|---|---|---|
| Aritmética | 631 chars | 208 | 26 s |
| Instrucciones | 1.101 chars | 319 | 39 s |
| Razonamiento | 212 chars | 73 | 10 s |
| Código | 1.995 chars | 546 | 67 s |
| Español | 2.798 chars | 705 | **87 s** |

Ochenta y siete segundos para producir dos frases.

> ⚠️ **Trampa que nos costó una tanda de pruebas entera.**
> Con `max_tokens` de 32–160, el modelo devolvía `content` **vacío**: agotaba todo el
> presupuesto dentro del bloque de razonamiento y no llegaba a emitir la respuesta.
> No es un fallo del modelo ni de la build. Con modelos razonadores hay que dar
> `max_tokens` holgado (600–900) o interpretarás un truncamiento como una alucinación.

## Estado del soporte (importante)

El soporte de esta arquitectura **no está mergeado en `llama.cpp`**. Hay varios PRs
abiertos y ninguno aceptado; el que se usó aquí está marcado como inestable. Dos
consecuencias visibles en el log de arranque:

- **Operaciones fusionadas desactivadas.** El backend Vulkan no implementa los kernels
  fusionados que esta arquitectura espera, y `llama.cpp` cae a la ruta genérica.
  **Parte de la lentitud medida es soporte inmaduro, no un techo del hardware.**
- **Una capa ignorada.** La última capa se descarta entera por "tensor no usado",
  incluidos los bloques de predicción multi-token (`nextn`). Es decir: se está
  evaluando el modelo **sin una de sus piezas**.

Por las dos razones, estas cifras deben leerse como un **suelo**, no como el rendimiento
definitivo del modelo en esta máquina.

## Nota de método: hay que parar el otro modelo

93 GB + 87 GiB no caben en 124 GB. El primer intento de carga falló con:

```
radv/amdgpu: Failed to allocate a buffer
```

No es un bug ni un límite de RADV: es aritmética. Hay que parar el servicio de
producción antes de cargar el segundo modelo, y restaurarlo después.

## Conclusión

**No sustituye al modelo en producción.** Se paga 3,3× en velocidad de generación y
2,4× en prefill, más una latencia real muy superior por el razonamiento extenso, a
cambio de una calidad que en estas pruebas no demostró ser mejor.

Dicho eso, el experimento responde algo valioso: **la cuantización extrema no era el
problema; los parámetros activos sí**. Un modelo de 313B a 1 bit razona correctamente,
pero en una máquina limitada por ancho de banda paga el precio de activar 18B por token.

**Siguiente candidato:** `REAP50-IQ4_XS` (88 GB) — el mismo modelo con el **50% de los
expertos podados** y cuantización IQ4 en lugar de IQ1. Hipótesis doble: menos expertos
activos ⇒ más rápido, y más bits por peso ⇒ mejor calidad. Si esa hipótesis se cumple,
sería la refutación limpia de la idea de que "más grande siempre es mejor".
