# MoE vs denso en memoria unificada

## Los datos

| Modelo | Totales | Activos | Tipo | Tamaño | pp t/s | tg t/s |
|---|---|---|---|---|---|---|
| Qwen3-8B | 8B | 8B | denso | 4,7 GB | 1.286,8 | **45,4** |
| Qwen3.8-27B | 27B | 27B | denso | ~16 GB | 365,1 | **13,1** |
| Qwen3.8-Flash-Next | 177B | ~3B | MoE | 87 GB | 299,8 | **27,4** |

## Lo contraintuitivo

El modelo de **177B genera al doble de velocidad** que el de 27B, aunque ocupe cinco veces más memoria.

## Por qué

En generación, cada token exige releer de memoria los pesos **activos**. Con memoria unificada (ancho de banda muy por debajo del de una GPU dedicada), ese tráfico es el limitante absoluto — el cómputo sobra.

- El denso de 27B lee **27B de parámetros** por token.
- El MoE de 177B enruta a unos pocos expertos y lee **~3B** por token.

Nueve veces menos tráfico. Que el modelo pese 87 GB solo significa que necesitas sitio para tenerlo cargado; no que cada token cueste 87 GB de lecturas.

En **prefill** la relación se invierte parcialmente: ahí se procesan muchos tokens a la vez, hay trabajo suficiente para saturar el cómputo y el denso pequeño arrasa (1.286 t/s del 8B).

## Regla para elegir modelo en Strix Halo

1. **Descarta los densos medianos (20B-70B).** Ni caben cómodos ni activan poco. Es el peor punto de la curva.
2. **Para calidad: MoE grande** con muchos parámetros totales y pocos activos. La memoria unificada es exactamente para esto.
3. **Para velocidad pura: denso pequeño** (≤8B), que corre a 45+ t/s.
4. **Mira los parámetros activos, no el tamaño del fichero.** Es el único número que predice los t/s de generación.

## Corolario sobre cuantización

Con 124 GB útiles caben modelos enormes a cuantización agresiva. Pero un MoE gigante a ~1 bit puede rendir **peor en calidad** que uno mediano a 4 bits, y no siempre compensa. La comparación pendiente en este repo es justamente esa: modelo enorme muy cuantizado vs modelo con expertos podados y cuantización decente.
