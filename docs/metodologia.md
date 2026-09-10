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
   Mínimo 3 medidas. Se reporta la mediana. Antes de las medidas se hace **una pasada de calentamiento explícita, que se registra y se descarta**: la primera petición de cada tamaño paga la reserva del KV cache y las páginas frías. Los scripts (`--passes N`) hacen N medidas *más* el calentamiento; el registro crudo marca cada petición con `warmup: true/false` y con su **fase** (`calentamiento` / `medida` / `needle`), así que la mediana es recalculable desde el JSONL sin fiarse del resumen y sin mezclar una petición de recuperación con las de rendimiento.

   El calentamiento tiene que **completarse**: si falla, se reintenta un número limitado y declarado de veces y, si sigue fallando, se **aborta ese punto** con código de salida propio. Un calentamiento fallido seguido de medidas que responden no es un punto válido —no hay garantía de que esas medidas sean calientes— y no puede terminar en un resumen que diga que todo pasó.

5. **Una medida solo cuenta si la respuesta es válida.**
   No basta con que el servidor devuelva HTTP 200. Un `{}`, un cuerpo sin `timings`, o un `content` vacío con todo el presupuesto gastado en razonamiento **no son una medida de 0 t/s: son un fallo**. Los validadores están en [`../scripts/validacion.py`](../scripts/validacion.py) y el contrato depende del tipo de prueba:

   | Tipo de prueba | Qué se exige |
   |---|---|
   | Comprobación exacta (p. ej. `17*23`) | `content.strip()` idéntico al valor esperado **y** finalización normal (`finish_reason: stop`). `-391`, `No es 391`, `391%` o `391 unidades` son FALLO. |
   | Tarea con resultado comprobable | Respuesta final válida y resultado correcto, no solo que haya timings. |
   | Medición de rendimiento con longitud fijada | Se **admite** `finish_reason: length` a propósito, registrando los tokens realmente generados. No se presenta como tarea completada. |
   | Llamada a herramientas | Se valida la llamada y sus argumentos; no se exige texto adicional. |
   | Recuperación de un dato enterrado (aguja) | Contrato **propio**, no el de medición: la clave tiene que ser la respuesta (se tolera solo puntuación final y comillas, y queda declarado), con finalización normal. Se distingue "la cita en prosa" (fallo de formato, recuperó) de "no la dice" (no leyó la ventana). Usar aquí el contrato de medición aceptaba una respuesta truncada por presupuesto, que es justo el caso en que el modelo **no** llegó a decir la clave. |

   Es decir: `finish_reason: stop` **no** se exige universalmente. Exigirlo en un banco de rendimiento con `max_tokens` fijado marcaría como fallo el comportamiento correcto. Pero su **ausencia** tampoco pasa: donde el contrato pide finalización normal, un `finish_reason` que no viene es un dato incompleto, no un aprobado. Y "exacto" significa exacto: `391.` es FALLO, porque el enunciado pedía solo el número.

   Las métricas también tienen contrato: velocidades finitas y plausibles (no `inf`, `NaN`, negativas ni cero), recuentos enteros y no negativos, y campos obligatorios presentes. Un `predicted_per_second` de infinito no es una medida rápida: es una medida corrupta.

6. **Fallo del modelo ≠ fallo del instrumento.**
   Un timeout, un sandbox que no arranca, un compilador ausente o un 502 del proxy **no** son "el modelo lo hizo mal": son que la prueba no se pudo ejecutar. Se cuentan aparte y tienen código de salida propio. En `verifica-codigo.py`: `2` = el modelo falló, `3` = el banco no pudo evaluar. Confundirlos infla o hunde la nota del modelo por motivos ajenos a él.

7. **Los fallos no se borran.**
   Las peticiones fallidas quedan en el registro crudo y en la tasa de fallos. No se repite una medida "hasta que salgan 5 buenas": eso convierte una tasa de error del 20% en un 0% aparente.

8. **Los scripts salen con código distinto de cero cuando algo falla.**
   Antes salían `0` con los fallos meramente anotados en una tabla, así que cualquier automatización los daba por buenos. Verificado con pruebas: [`../tests/`](../tests/).

9. **Desactivar reutilización de caché al medir prefill.**
   Si el servidor reaprovecha el prefijo del prompt, el segundo `pp` sale absurdamente alto y no mide nada.

10. **Anotar versiones.**
    Cada fila del CSV lleva la fecha y el commit corto de `llama.cpp`. Mesa y kernel cambian el resultado; sin esa referencia, un número es folclore.

## Lo que NO se hace

- No se publican cifras estimadas como si fueran medidas. Lo no medido va como `⏳` o marcado explícitamente como estimación.
- No se borra un resultado que resultó equivocado: se marca como refutado y se enlaza la corrección.
- No se comparan modelos con distinta cuantización sin decirlo.
- No se afirma equivalencia desde la ausencia de diferencia: "no se observó diferencia con N pasadas" no es "son iguales".

## Scripts y batería

- [`../scripts/bench-ubatch.py`](../scripts/bench-ubatch.py) automatiza el ciclo: construye un prompt del tamaño pedido, lanza el calentamiento y N pasadas por cada `ubatch`, reinicia el servicio entre puntos, restaura el servicio productivo comprobando su estado previo y vuelca tabla + JSONL crudo.
- [`../scripts/validacion.py`](../scripts/validacion.py) contiene los contratos por tipo de prueba.
- [`../benchmarks/bateria-publica.json`](../benchmarks/bateria-publica.json) es la batería reproducible: enunciados, entradas y resultados esperados, con datos sintéticos y sin nada privado. Sustituye a la batería que vivía en `private/` y que hacía las pruebas irrepetibles desde fuera.
- [`../tests/`](../tests/) son las pruebas del propio instrumento: comprueban que las respuestas malas (`{}`, sin timings, `-391`, `No es 391`, contenido vacío, finalización por límite, fallo de restauración) fallan como deben, y que el código de salida del proceso lo refleja. Se ejecutan con `python3 -m unittest discover -s tests`. Cubren también el verificador de código con `tsc`, `node` y `sqlite3` **reales** (no simulando el compilador), el flujo completo desde una copia limpia del repo sin `private/`, y los errores de la recuperación de aguja. Criterio de aceptación de una prueba nueva: tiene que **fallar** contra el commit anterior al arreglo. Si pasa en los dos, no está probando el arreglo.
