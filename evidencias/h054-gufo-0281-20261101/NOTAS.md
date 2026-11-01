# H-054 — Gufo 0.17.0 -> 0.28.1 en produccion (01-nov-2026)

Mismo modelo (Qwen3.8-Flash-Next UD-Q4_K_XL + MTP), misma unidad, solo cambia la imagen de Gufo.
Gufo 0.28.1 ya no escribe `event=completed` en el log: las velocidades salen de `usage`
(`prompt_tokens_per_second`, `completion_tokens_per_second`, `draft_tokens*`) -> `real-usage.py`.
Corpus = ficheros del commit a91ff6a (los de H-049), salvo que prosa-resume/hallazgos crecen un poco.

## Velocidad (generacion t/s, pasada caliente) — referencia: h049b real-gufo-d2 / d3 (0.17.0)
| tarea | 0.17 d2 | 0.17 d3 | 0.28.1 |
|---|---|---|---|
| codigo-explica | 37,2 | 22,8 | 43,3 |
| codigo-cambio | 33,9 | 34,7 | 41,3 |
| codigo-largo | 41,6 | 41,1 | 49,3 |
| prosa-resume | 33,5 | 33,2 | 37,8 |
| prosa-hallazgos (45-48k) | 38,3 | 38,0 | 43,3 |
| json-extrae | 53,3 | 63,0 | 91,0 |
Prefill en frio 1.412-1.531 t/s (0.17: 1.280-1.501). Aceptacion MTP algo menor (53-63 % vs 62-75 %) y aun asi genera mas.

## Calidad (bench-calidad-motores.py HEAD, --bust-cache): 11/12
Unico fallo `instruccion-negativa` ("temperatura"). Con el prompt EXACTO de H-053 (sin run-id) pasa;
con 5 run-id distintos falla 2/5 (`neg.py`). Es una celda sensible al prefijo; NO medido si la 0.17
fallaba igual con run-id (H-053 corrio sin --bust-cache). Las dos agujas (48k) pasan sin cache.
