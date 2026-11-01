# H-056 — Qwen3.8-Flash-Next Uncensored (vmlinux Gufo-Q4Mix, base orcarouter) vs produccion (01-nov-2026)

Mismo Gufo 0.28.1, mismo MTP sidecar Q8_0, mismos flags; mmproj BF16 del propio repo. Fichero 109.776.678.624 B,
SHA-256 c7f23312...4b7a = el publicado. Ventana: produccion parada, uncensored en :8081, restauracion por trap.

| prueba | produccion UD-Q4_K_XL (H-054/H-055) | uncensored Q4Mix |
|---|---|---|
| negativas a 8 peticiones delicadas legales | 7/8 | 0/8 |
| calidad 12 (bust-cache) | 11/12 | 11/12 (mismo fallo: instruccion-negativa) |
| celdas dudosas x3 | 4/4 estable | 4/4 estable |
| fugas / vision / tools | 0 / OK / OK | 0 / OK / OK |
| concurrencia 1/2/4 t/s | 60,8 / 98,7 / 103,1 | 62,6 / 100,8 / 103,7 |
| contexto 1,5k-45k aguja / pp | 4/4 / 1.520-1.626 | 4/4 / 1.505-1.612 |
| bateria 27 (tg mediana) | 27/27, 54,6 | 27/27, 55,2 |
| codigo compilado | 4 pasan, 2 sin banco, 0 fallan | igual |
| velocidad real (6 tareas, tg) | 43,3/41,3/49,3/37,8/43,3/91,0 | 42,9/58,4/44,7/40,8/38,9/90,8 |

Lectura: en este banco NO se ve perdida. NO medido: veracidad (TruthfulQA), que es donde la literatura
situa la caida de las versiones abliteradas. Las respuestas sin censura no se publican aqui (solo el recuento).
