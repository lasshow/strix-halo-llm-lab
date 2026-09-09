# Ventana de contexto: 256k servidos, 33k verificados

> **Aviso de honestidad.** El servidor **arranca y reserva** la ventana completa de
> 262.144 tokens, y eso está comprobado (`n_ctx_slot = 262144` en el log). Lo que **no**
> está comprobado es el comportamiento *usando* esa ventana: el prompt más largo que he
> medido de verdad es de **~33.000 tokens**. Entre 33k y 256k no tengo datos, y encima hay
> un cuelgue observado alrededor de 90k (ver más abajo). Trata la cifra de 256k como
> **capacidad reservada**, no como capacidad validada.

## Lo que está servido

- `context_length` nativo del modelo: **262.144 tokens** (256k).
- RoPE `freq_base`: **10.000.000** — el modelo está entrenado para esa ventana, no es una extensión a posteriori.
- El servidor arranca con `-c 262144`, es decir, **la ventana completa**, sin recortar.
- Con **caché KV unificada** y 2 slots concurrentes, cada slot reporta `n_ctx_slot = 262144`. Sin unificar, cada slot se quedaría con la mitad.
- `--no-context-shift`: al llegar al límite se corta, no se descarta silenciosamente el principio de la conversación. Preferible a que el modelo "olvide" sin avisar.

## Por qué no explota la memoria

En un transformer denso clásico, la caché KV crece linealmente con el contexto y con el número de capas de atención. A 256k eso serían decenas de GB.

Aquí no, porque la arquitectura es **híbrida**. De sus **48 capas**:

- **12 son de atención** — y encima con solo **2 cabezas KV** (atención agrupada muy agresiva) más un **indexador disperso** que selecciona los ~2.048 tokens más relevantes en lugar de atender a todos.
- **36 son capas lineales de estado fijo** (dimensión 128). Su coste en memoria **no depende de la longitud del contexto**: mantienen un estado de tamaño constante.

Resultado: el consumo de KV crece con el contexto de forma casi plana comparado con un denso equivalente. Es lo que hace que 256k sea una cifra real y no marketing.

## Ajustes relacionados

| Parámetro | Valor | Para qué |
|---|---|---|
| `--cache-reuse` | 256 | Reaprovecha prefijos comunes entre peticiones (útil en chat) |
| `--cache-ram` | 4096 MB | Techo de caché de prompt en RAM. Bajado de 24576: con `--lazy-mode off` los pesos son residentes y 24 GB de caché provocaban OOM |
| `--no-context-shift` | activo | Corta en el límite en vez de descartar el inicio |
| `-kvu` (KV unificada) | activo | Los slots comparten pool en vez de partirlo |

## Aviso pendiente de verificar

Con `ubatch` alto se observó un **cuelgue de la GPU alrededor de los 90k tokens** de
prefill. No está reverificado y puede depender de la versión de Mesa o del driver. Si vas
a trabajar rutinariamente por encima de ~64k, prueba con `ubatch` más bajo (512-1024) y
comprueba estabilidad antes de fiarte.

Este cuelgue es la razón de que la configuración de producción use `--ubatch-size 2048` y
no 4096: el barrido daba un **+1% marginal** al subir a 4096, insuficiente para justificar
acercarse a una zona con inestabilidad conocida y mayor consumo de memoria.

## Lo que falta por medir

- [ ] Prefill real a 64k, 128k y 256k (ahora mismo solo hay datos hasta 33k)
- [ ] Reverificar el cuelgue de ~90k con `--ubatch-size 2048` y Mesa actualizada
- [ ] Consumo de KV medido a contexto lleno, no estimado
