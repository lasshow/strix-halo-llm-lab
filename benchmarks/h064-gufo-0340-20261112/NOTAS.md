# H-064 — Gufo 0.30.0 -> 0.34.0 (12-nov-2026)

Mismo modelo (Qwen3.8-Flash-Next UD-Q4_K_XL + MTP), misma unidad, mismo kernel (7.2.15). Imagen nueva
`sha256:49b1fce40cb43e33c46d91595e300cb320cdec768fcd976f8c8da24b402fc133`, promovida por el cron nocturno
(puerta: 75,3 -> 74,9 t/s, mediana de 3). Referencia: H-060 (0.30.0), la ultima medida con la ronda completa;
0.31.0-0.33.0 pasaron solo la puerta del cron, sin ronda: no hay cifras propias de ellas.

## Carga real (generacion t/s, corpus a91ff6a)
| tarea | 0.30.0 | 0.34.0 | acept MTP |
|---|---|---|---|
| codigo-explica | 44,1 | 44,0 | 64,8 % (igual) |
| codigo-cambio | 55,2 | 54,3 | 79,8 % (igual) |
| codigo-largo | 49,3 | 49,4 | 61,1 % (igual) |
| prosa-resume | 43,0 | 43,1 | 59,0 % (igual) |
| prosa-hallazgos (48k) | 39,1 | 39,3 | 55,7 % (igual) |
| json-extrae | 90,5 | 89,2 | 96,1 % (igual) |
Prefill en frio 1.488-1.579 t/s (0.30.0: 1.488-1.604). Aceptaciones MTP identicas al digito otra vez.

## Rondas
| ronda | 0.30.0 | 0.34.0 |
|---|---|---|
| fugas entre 8 conversaciones | 0 | 0 (8/8 identicas a serie) |
| concurrencia 1 / 2 / 4 (t/s agregados) | 60,1 / 90,3 / 92,5 | 60,6 / 86,2 / 92,4 |
| vision / tool call | OK / OK | OK / OK |
| celdas dudosas x3 | 4/4 estable | 4/4 estable |
| contexto 2k-45k pp (t/s) y aguja | 1.554-1.674, 4/4 | 1.559-1.667, 4/4 |
| calidad 12 celdas --bust-cache | 12/12 | 11/12 (ver abajo) |
| bateria 27 (sin razonamiento) tg / pp mediana | 54,5 / 386,5 | 54,4 / 373,4 |
| codigo compilado (C1-C6) | 4 pasan, 2 compilan sin banco | 4 pasan, 2 compilan sin banco |

## Calidad 11/12: celda sensible al prefijo, no regresion
Falla `instruccion-negativa` (escribe "temperatura"). Repetida 3 veces mas con --bust-cache: NO / OK / NO
(2 de 4 tomas fallan). Es la celda sensible al prefijo ya documentada en H-054 y H-056 (11/12 con este mismo
fallo); en celdas x3 sin prefijo sale 3/3 estable. Atribuible al modelo, no al motor.

## Concurrencia (hipotesis abierta desde H-060)
Concurrencia 2 sigue por debajo de 0.29.0 (101,4 -> 90,3 -> 86,2); 4 igual que 0.30.0. Una pasada cada una:
sin medir x3 no se puede separar del ruido. Uso real = 1-2 slots, no justifica rollback.

## Errores del banco en esta ronda (declarados)
- verifica-codigo dio C4 "NO COMPILA" y C3 "herramienta ausente": el bind mount ro /opt/tc/node_modules
  apuntaba a un node_modules BORRADO por `hermes update` (findmnt: `.../node-deps-discard-.../node_modules//deleted`).
  Remontado y re-verificadas las MISMAS respuestas: 4 pasan + 2 compilan, identico a 0.30.0. El script
  ahora detecta el montaje rancio y lo remonta solo.
- N1 (zig) y N2 (go) sin verificador en el A8 (zig vivia en /tmp); igual que en H-060, no cuentan.

## Veredicto
0.34.0 = neutro frente a 0.30.0 en todo lo medido (serie dentro del 1-2 %, MTP identico, aguja 4/4, codigo
identico). Se queda en produccion. Sigue abierta la sombra de concurrencia 2.
