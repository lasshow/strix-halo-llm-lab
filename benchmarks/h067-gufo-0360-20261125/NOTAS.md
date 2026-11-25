# H-067 — Gufo 0.34.0 -> 0.36.0 + kernel 7.3.4 + Mesa 26.2.7 (25-nov-2026)

Mismo modelo (Qwen3.8-Flash-Next UD-Q4_K_XL + MTP), misma unidad, mismo corpus (a91ff6a). Cambian a la vez
motor (0.34.0 -> 0.35.0 -> 0.36.0, promovidos por la puerta nocturna), kernel (7.2.15 -> 7.3.4) y Mesa (26.2.6 -> 26.2.7):
las mejoras NO se pueden atribuir a una sola capa. Referencia: H-064. Runner: `scripts/h067-runner.sh`
(= H-059/H-064 + concurrencia repetida, x3 en total).

## Carga real (generacion t/s, pasada caliente)
| tarea | 0.34.0 | 0.36.0 | acept MTP |
|---|---|---|---|
| codigo-explica | 44,0 | 45,1 | 64,9 % (64,8) |
| codigo-cambio | 54,3 | 58,7 | 78,9 % (79,8) |
| codigo-largo | 49,4 | 51,3 | 62,2 % (61,1) |
| prosa-resume | 43,1 | 43,7 | 58,7 % (59,0) |
| prosa-hallazgos (48k) | 39,3 | 39,9 | 55,6 % (55,7) |
| json-extrae | 89,2 | 95,2 | 96,1 % (igual) |
Prefill en frio 1.467-1.606 t/s (0.34.0: 1.488-1.579).

## Rondas
| ronda | 0.34.0 | 0.36.0 |
|---|---|---|
| fugas entre 8 conversaciones | 0 | 0 (8/8 identicas a serie) |
| concurrencia 1 (t/s) | 60,6 | 61,0 / 64,6 / 64,6 |
| concurrencia 2 (t/s agregados) | 86,2 (1 pasada) | 106,1 / 107,6 / 109,2 |
| concurrencia 4 (t/s agregados) | 92,4 | 105,8 / 108,2 / 107,0 |
| vision / tool call | OK / OK | OK / OK |
| celdas dudosas x3 | 4/4 estable | 4/4 estable |
| contexto 1,5k-45k pp (t/s) y aguja | 1.559-1.667, 4/4 | 1.570-1.687, 4/4 |
| calidad 12 celdas --bust-cache | 11/12 | 11/12 (misma celda) |
| bateria 27 (sin razonamiento) tg / pp mediana | 54,4 / 373,4 | 55,0 / 382,9 |
| codigo compilado (C1-C6) | 4 pasan, 2 compilan sin banco | 4 pasan, 2 compilan sin banco |

## Concurrencia: sombra de H-060/H-064 cerrada
c=2 x3 = 106,1-109,2 t/s (dispersion 3 %), por encima de 0.34.0 (86,2) y de 0.29.0 (101,4). c=4 tambien sube
(~92 -> ~107). La caida de 0.30-0.34 ya no existe con esta combinacion; no se sabe cual de las tres capas la quito.

## Calidad 11/12
Falla `instruccion-negativa` con prefijo (escribe "temperatura"), igual que H-054/H-056/H-064; en celdas x3 sin
prefijo sale 3/3 estable. Celda sensible del modelo, no regresion.

## Limites declarados
- verifica-codigo rc=3: N1 (zig) y N2 (go) sin verificador en el A8, igual que H-060/H-064; no cuentan.
- Concurrencia 1/2/4 x3, el resto de rondas 1 pasada (como en H-064).
- Ronda corrida con produccion viva (sin trafico ajeno conocido durante los 12 min).

## Veredicto
0.36.0 + kernel 7.3.4 = igual o mejor en todo: codigo-cambio +8 %, json +7 %, concurrencia 2 +24 % (x3), sin
regresiones de calidad. Se queda en produccion.
