# H-060 — Gufo 0.29.0 -> 0.30.0 (05-nov-2026)

Mismo modelo (Qwen3.8-Flash-Next UD-Q4_K_XL + MTP), misma unidad, mismo kernel (7.2.15). Cambia solo la
imagen de Gufo: digest nuevo `sha256:9d22f4ca87d8406ffbfadab283fe24d08e670c66a8b01e357ea4080e5da50ba0`
(viejo en H-059: `9d77753a…`). Medido por la puerta de produccion (:8080, llama-swap). Referencia: H-059.

## Carga real (generacion t/s, corpus a91ff6a)
| tarea | 0.29.0 | 0.30.0 | acept MTP |
|---|---|---|---|
| codigo-explica | 44,3 | 44,1 | 64,8 % (igual) |
| codigo-cambio | 55,4 | 55,2 | 79,8 % (igual) |
| codigo-largo | 49,6 | 49,3 | 61,1 % (igual) |
| prosa-resume | 43,1 | 43,0 | 59,0 % (igual) |
| prosa-hallazgos (48k) | 39,3 | 39,1 | 55,7 % (igual) |
| json-extrae | 90,5 | 90,5 | 96,1 % (igual) |
Prefill en frio 1.488-1.604 t/s (0.29.0: 1.437-1.578). Aceptaciones MTP IDENTICAS al digito: el borrador
no cambió; las diferencias son ruido de maquina.

## Rondas
| ronda | 0.29.0 | 0.30.0 |
|---|---|---|
| fugas entre 8 conversaciones | 0 | 0 (8/8 identicas a serie) |
| concurrencia 1 / 2 / 4 (t/s agregados) | 60,7 / 101,4 / 103,0 | 60,1 / 90,3 / 92,5 |
| vision / tool call | OK / OK | OK / OK |
| celdas dudosas x3 | 4/4 estable | 4/4 estable |
| contexto 2k-45k pp (t/s) y aguja | 1.620-1.678, 4/4 | 1.554-1.674, 4/4 |
| calidad 12 celdas --bust-cache | 12/12 | 12/12 |
| bateria 27 (sin razonamiento) tg / pp mediana | 54,6 / 399,8 | 54,5 / 386,5 |
| codigo compilado (C1-C6) | 4 pasan, 2 compilan sin banco | 4 pasan, 2 compilan sin banco (veredicto caso a caso identico) |

## Dato a vigilar (hipotesis, no probado)
Concurrencia 2 y 4 baja ~10 % (101,4->90,3 y 103,0->92,5) con una sola pasada cada una. En serial todo cuadra
al 0,5 %. Podria ser cambio real del planificador de slots en 0.30.0 o deriva termica/frecuencia en la toma.
Si en la proxima ronda se repite, medir concurrencia x3 antes de darlo por bueno.

## Errores del banco en esta ronda (declarados)
Ninguno: rc=0 en las 6 rondas a la primera, flags iguales que la referencia (--sin-razonamiento aplicado,
validacion.py copiado, corpus fijado a a91ff6a).

## Veredicto
0.30.0 = neutro en serial (todo dentro de ruido, MTP idéntico), 12/12 calidad y 4/4 aguja. Sombra única:
concurrencia multi-slot -10 % en una pasada. Se queda en produccion (uso real = 1-2 slots); revisar en la
proxima actualizacion si persiste.
