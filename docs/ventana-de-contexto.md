# Ventana de contexto: 256k reales

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
| `--cache-ram` | 24576 MB | Techo de caché de prompt en RAM |
| `--no-context-shift` | activo | Corta en el límite en vez de descartar el inicio |
| `-kvu` (KV unificada) | activo | Los slots comparten pool en vez de partirlo |

## Aviso pendiente de verificar

Con `ubatch` alto se observó un **cuelgue de la GPU alrededor de los 90k tokens** de prefill. No está reverificado y puede depender de la versión de Mesa o del driver. Si vas a trabajar rutinariamente por encima de ~64k, prueba con `ubatch` más bajo (512-1024) y comprueba estabilidad antes de fiarte.
