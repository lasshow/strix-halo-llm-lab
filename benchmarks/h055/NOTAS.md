# H-055 — Rondas completas contra produccion con Gufo 0.28.1 (01-nov-2026)

Referencia: H-049 (Gufo 0.17.0, 17-oct-2026), misma unidad y modelo (Qwen3.8-Flash-Next UD-Q4_K_XL + MTP).

| ronda | 0.17.0 | 0.28.1 |
|---|---|---|
| fugas entre 8 conversaciones | 0 | 0 (8/8 identicas a serie) |
| concurrencia 1 / 2 / 4 (t/s agregados) | 38,3 / 60,3 / 68,8 | 60,8 / 98,7 / 103,1 |
| vision (rojo/azul) | OK | OK |
| tool call get_weather(Bilbao) | OK | OK |
| celdas dudosas H-045 x3 (negativa, trampa, 2 agujas) | — | 4/4 ESTABLE OK |
| contexto 1,5k-45k con aguja | — | aguja 4/4, pp 1.520-1.626 t/s (no cae con el contexto) |
| codigo compilado y ejecutado (8 casos) | flashnext-v2: 3 pasan, 3 sin respuesta | 4 pasan, 2 compilan sin banco, 0 fallan |

bench-context da tg ~22 t/s (vs 43-49 en bench real): HIPOTESIS no verificada = no fija muestreo greedy
y el MTP acepta menos; no es comparable con las otras cifras de generacion.

Arreglos del banco en este hallazgo:
- bench-calidad.py: envia "model" (Gufo responde 400 missing_model sin el) y lee pp/tg de usage.
- verifica-codigo.py: C5 probaba con lineas "a","b" sin IP -> la respuesta que valida IPv4 (mas fiel al
  enunciado) salia COMPILA PERO FALLA. Ahora lineas de log reales; la respuesta antigua (flashnext-v2)
  sigue pasando. zig/go sin verificador ya no revientan con KeyError.
