# BIOS, carveout UMA y memoria

## El experimento

Hipótesis inicial: en una APU con memoria unificada, ampliar el *UMA Frame Buffer* de la BIOS (la porción reservada como VRAM) daría más rendimiento en inferencia.

**Resultado: no cambia nada.**

| Configuración | MemTotal | VRAM | GTT | pp t/s | tg t/s |
|---|---|---|---|---|---|
| Carveout mínimo | 62 GB | — | — | 1.279,19 | 45,34 |
| Carveout ampliado | 124,5 GB | 1 GB | 124 GB | 1.286,84 | 45,42 |

Diferencia: +0,6% en pp, +0,2% en tg. Ruido de medición.

## Por qué

RADV expone la memoria como un **pool único** (`uma:1`): suma VRAM reservada y GTT. Da igual cómo repartas en BIOS, la inferencia ve lo mismo. Lo que sí importa son los parámetros del kernel que fijan el techo de GTT.

**Conclusión práctica:** deja el carveout al mínimo. Reservar VRAM solo resta RAM al sistema.

## Navegar la BIOS (AMI, placas Sixunited AXB35)

| | |
|---|---|
| Entrar en la BIOS | `DEL` o `ESC` (**no** F12) |
| Menú de arranque | `F7` |
| Guardar y salir | **`F4`** (no F10 en esta AMI) |
| Ruta del carveout | Advanced → GFX Configuration → iGPU Configuration |

El tamaño del frame buffer **solo aparece** si cambias `iGPU Configuration` de `Auto` a `UMA_SPECIFIED`. En `Auto` la opción está oculta.

## Verificación tras cambiar

```bash
free -g                                  # RAM total disponible al sistema
vulkaninfo --summary | grep -i memory    # pool visible para Vulkan
```

Con el carveout mínimo y GTT bien configurado, Vulkan debe reportar prácticamente toda la memoria como utilizable.

## Nota sobre firmware

Comprueba la versión de BIOS **antes** de intentar actualizarla. Las versiones que publica el fabricante en su web pueden ser más antiguas que la que trae la placa de fábrica, y el número que muestra la BIOS AMI no siempre coincide con la nomenclatura comercial del vendedor. Si ya estás en la última, no flashees.
