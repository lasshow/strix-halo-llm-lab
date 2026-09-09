# MoE vs denso en memoria unificada

## Los datos

| Modelo | Totales | Activos | Tipo | Tamaño | pp t/s | tg t/s |
|---|---|---|---|---|---|---|
| Qwen3-8B | 8B | 8B | denso | 4,7 GB | 1.286,8 | **45,4** |
| Qwen3.8-27B | 27B | 27B | denso | ~16 GB | 365,1 | **13,1** |
| Qwen3.8-Flash-Next | 177B | ~3B | MoE | 87 GB | 299,8 † | **27,4** |
| GLM-5.3-Flash | 313B | ~18B | MoE | 93 GB | 124,8 | **8,3** |

## Lo contraintuitivo

El modelo de **177B genera al doble de velocidad** que el de 27B, aunque ocupe cinco veces más memoria.

Y el de **313B genera a un tercio** que el de 177B, aunque ocupe prácticamente lo mismo (93 vs 87 GB). El tamaño en disco no predice nada.

## Por qué

En generación, cada token exige releer de memoria los pesos **activos**. Con memoria unificada (ancho de banda muy por debajo del de una GPU dedicada), ese tráfico es el limitante absoluto — el cómputo sobra.

- El denso de 27B lee **27B de parámetros** por token.
- El MoE de 177B enruta a unos pocos expertos y lee **~3B** por token.
- El MoE de 313B enruta a bastantes más y lee **~18B** por token.

Nueve veces menos tráfico en el segundo caso. Que el modelo pese 87 GB solo significa que necesitas sitio para tenerlo cargado; no que cada token cueste 87 GB de lecturas.

La proporción se sostiene bien en la práctica: 3B activos → 27,4 t/s; 18B activos → 8,3 t/s. Seis veces más parámetros activos, algo más de tres veces más lento (el resto lo amortigua que parte del modelo es común a todos los expertos).

En **prefill** la relación se invierte parcialmente: ahí se procesan muchos tokens a la vez, hay trabajo suficiente para saturar el cómputo y el denso pequeño arrasa (1.286 t/s del 8B).

## Regla para elegir modelo en Strix Halo

1. **Descarta los densos medianos (20B-70B).** Ni caben cómodos ni activan poco. Es el peor punto de la curva.
2. **Para calidad: MoE grande** con muchos parámetros totales y pocos activos. La memoria unificada es exactamente para esto.
3. **Para velocidad pura: denso pequeño** (≤8B), que corre a 45+ t/s.
4. **Mira los parámetros activos, no el tamaño del fichero.** Es el único número que predice los t/s de generación.

## Corolario sobre cuantización (ya medido)

La sospecha inicial era que un MoE gigante a ~1 bit rendiría **peor en calidad** que uno mediano a 4 bits. **Resultó ser falsa en la parte de calidad y cierta en la de velocidad.**

GLM-5.3-Flash a `UD-IQ1_S` (≈1 bit por peso) acertó las 5 pruebas de coherencia: aritmética, seguimiento de instrucciones, razonamiento temporal, código y redacción en español. No se degradó de forma apreciable. Lo que lo descarta para uso diario **no es la cuantización, son sus 18B activos**.

👉 Detalle completo en [`glm53-flash.md`](glm53-flash.md).

**Siguiente comparación pendiente:** la variante `REAP50-IQ4_XS` (88 GB) — mismo modelo con el 50% de expertos podados y cuantización IQ4. Si podar expertos reduce los parámetros activos, debería ser a la vez más rápido *y* más preciso que el IQ1_S. Sería la prueba limpia de que en esta máquina conviene optimizar activos, no bits.

† Prefill medido con la carga diferida activa (por defecto). Con `--lazy-mode off` sube a **415,4 t/s**; ver [`../docs/carga-diferida-y-oom.md`](../docs/carga-diferida-y-oom.md). La comparación entre modelos sigue siendo válida: todos se midieron en las mismas condiciones.
