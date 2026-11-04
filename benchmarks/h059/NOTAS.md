# H-059 — Gufo 0.28.1 -> 0.29.0 + kernel 7.2.14 -> 7.2.15 (04-nov-2026)

Mismo modelo (Qwen3.8-Flash-Next UD-Q4_K_XL + MTP), misma unidad. Cambian la imagen de Gufo (digest en
entorno.txt), el kernel y los paquetes de Fedora. Todo medido por la puerta de produccion (:8080, llama-swap,
sin sobrecoste segun H-058). Referencias: H-054 (carga real, calidad) y H-055 (rondas).

## Carga real (generacion t/s, pasada caliente, corpus a91ff6a)
| tarea | 0.28.1 | 0.29.0 | acept MTP 0.29 |
|---|---|---|---|
| codigo-explica | 43,3 | 44,3 | 64,8 % |
| codigo-cambio | 41,3 | 55,4 | 79,8 % |
| codigo-largo | 49,3 | 49,6 | 61,1 % |
| prosa-resume | 37,8 | 43,1 | 59,0 % |
| prosa-hallazgos (48k) | 43,3 | 39,3 | 55,7 % |
| json-extrae | 91,0 | 90,5 | 96,1 % |
Prefill en frio 1.437-1.578 t/s (0.28.1: 1.412-1.531).
Salvedad: en H-054 prosa-resume/prosa-hallazgos ya se avisaba de que su corpus crece; no leer esas dos filas
como cambio del motor. La unica mejora clara es codigo-cambio (+34 %), con aceptacion MTP 61 -> 80 %.

## Rondas (H-055)
| ronda | 0.28.1 | 0.29.0 |
|---|---|---|
| fugas entre 8 conversaciones | 0 | 0 (8/8 identicas a serie) |
| concurrencia 1 / 2 / 4 (t/s agregados) | 60,8 / 98,7 / 103,1 | 60,7 / 101,4 / 103,0 |
| vision / tool call | OK / OK | OK / OK |
| celdas dudosas x3 | 4/4 estable | 4/4 estable |
| contexto 1,5k-45k pp (t/s) y aguja | 1.520-1.626, 4/4 | 1.620-1.678, 4/4 |
| calidad 12 celdas --bust-cache | 11/12 | 12/12 |
| bateria 27 (sin razonamiento) tg / pp mediana | 54,6 / 394,8 | 54,6 / 399,8 |
| codigo compilado (C1-C6) | 4 pasan, 2 compilan sin banco | 4 pasan, 2 compilan sin banco |

instruccion-negativa pasa esta vez, pero es una sola pasada de una celda sensible al prefijo (H-054): no se
da por arreglada.

## Errores del banco en esta ronda (declarados)
1. bench-context fallo la primera vez: el runner no copio `validacion.py` al M5. Repetido.
2. La bateria de 27 se lanzo SIN `--sin-razonamiento` (la referencia es sin el): 5 de 6 casos de codigo
   razonaron hasta agotar 4096 tokens y devolvieron vacio (bateria-con-razonamiento-ERROR.txt). Medida
   descartada, no es fallo de Gufo.
3. Al repetirla, bench-calidad reanudo desde el resp-*.json viejo y salto todo; hubo que apartarlo.
   Pendiente: que bench-calidad guarde los flags en el fichero y se niegue a reanudar si no coinciden.

## Veredicto
0.29.0 = igual o mejor en todo lo medido, sin regresiones: prefill +3-6 %, codigo-cambio +34 %, el resto en
ruido. Queda en produccion.
