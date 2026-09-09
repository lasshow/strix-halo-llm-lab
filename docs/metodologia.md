# Metodología

Cómo se toman las medidas de este repositorio, y por qué así.

## Qué se mide

- **pp (prompt processing / prefill)** — tokens por segundo procesando el prompt de entrada. Dominado por cómputo; escala bien con `ubatch`.
- **tg (token generation)** — tokens por segundo generando la respuesta. Dominado por el **ancho de banda de memoria**: hay que releer los pesos activos en cada token. Aquí es donde se decide si un modelo es usable.

## Reglas

1. **Medir contra el servidor real, no con `llama-bench` de otro modelo.**
   Es el error nº1 y produce recomendaciones invertidas (ver hallazgo H-005). Las cifras de este repo salen de peticiones HTTP reales a `llama-server`, leyendo el bloque `timings` de la respuesta.

2. **Prompt realista.**
   Un prompt de 50 tokens no dice nada del comportamiento a contexto largo. El barrido de `ubatch` usa un prompt de **~33.000 tokens**.

3. **Reinicio entre configuraciones.**
   Cambiar `ubatch` implica reiniciar el servicio. Sin reinicio se arrastran caché de prompt y estado de la GPU, y los puntos dejan de ser comparables.

4. **Varias pasadas por punto.**
   Mínimo 3. Se reporta la mediana. La primera pasada tras un reinicio suele ser más lenta (páginas frías) y se descarta.

5. **Desactivar reutilización de caché al medir prefill.**
   Si el servidor reaprovecha el prefijo del prompt, el segundo `pp` sale absurdamente alto y no mide nada.

6. **Anotar versiones.**
   Cada fila del CSV lleva la fecha y el commit corto de `llama.cpp`. Mesa y kernel cambian el resultado; sin esa referencia, un número es folclore.

## Lo que NO se hace

- No se publican cifras estimadas como si fueran medidas. Lo no medido va como `⏳` o marcado explícitamente como estimación.
- No se borra un resultado que resultó equivocado: se marca como refutado y se enlaza la corrección.
- No se comparan modelos con distinta cuantización sin decirlo.

## Script

[`../scripts/bench-ubatch.py`](../scripts/bench-ubatch.py) automatiza el ciclo: construye un prompt del tamaño pedido, lanza N pasadas por cada `ubatch`, reinicia el servicio entre puntos y vuelca una tabla.
