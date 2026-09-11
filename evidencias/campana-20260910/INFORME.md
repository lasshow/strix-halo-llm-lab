# Campana nocturna M5 — 10/11-sep-2026

Duracion: 25.6 min · produccion al final: OK

## Cambios aplicados a llama-flashnext
- llama.cpp 311d421 -> df03399
- --cache-ram 4096 -> 12288

Smoke: respuesta=True · 401=True · vision=True · '391'
Produccion final: pp(3k) 336.2 t/s · tg 26.86 t/s

## F1 build 311d421 vs df03399
- old: pp3k 330.8 · tg3k 26.74 · pp24k 360.5 · tg24k 23.85
- new: pp3k 335.4 · tg3k 26.86 · pp24k 361.8 · tg24k 23.97
-> adoptar build nueva: True

## F2 especulacion (tg t/s por familia · acceptance %)
- control: prosa 27.67 · codigo 27.65 · json 27.68 · creativo 27.70 · pp3k 333.4
- mtp-n2-p0: ERROR [F2-mtp-n2-p0] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.597.612 I common_specu
- mtp-n3-p0: ERROR [F2-mtp-n3-p0] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.682.129 I common_specu
- mtp-n3-p06: ERROR [F2-mtp-n3-p06] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.696.329 I common_spec
- mtp-n5-p06: ERROR [F2-mtp-n5-p06] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.699.125 I common_spec
- ngram-simple: prosa 27.67 · codigo 26.31 (29.6%) · json 27.70 · creativo 27.70 · pp3k 336.6 · ganancia 0.976x peor 0.951x
-> adoptar: None

## F3 DPM
- auto: prompt_ms 820 · idle 6.6 W | high: prompt_ms 816 · idle 14.4 W -> adoptar high: False

## Notas / incidencias
- F1/old: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/old: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/old: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/old: prompt_n real 18038 vs objetivo 24000 (>15%)
- F1/new: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/new: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/new: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/new: prompt_n real 18038 vs objetivo 24000 (>15%)
- F1/old: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/old: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/old: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/old: prompt_n real 18038 vs objetivo 24000 (>15%)
- F1/new: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/new: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/new: prompt_n real 2294 vs objetivo 3000 (>15%)
- F1/new: prompt_n real 18038 vs objetivo 24000 (>15%)
- F2/control: prompt_n real 2294 vs objetivo 3000 (>15%)
- F2/control: prompt_n real 2294 vs objetivo 3000 (>15%)
- F2 mtp-n2-p0: [F2-mtp-n2-p0] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.597.612 I common_speculative_init_result: creating MTP draft context against the target model '/models/gguf/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf'
0.17.597.622 W llama_init_from_model: context type MTP requested but model doesn't contain MTP layers
0.17.597.622 E common_speculative_init_result: failed to create MTP context
0.17.597.628 E srv    load_model: failed to create MTP context
0.17.597.632 I srv    operator(): operator(): cleaning up before exit...
0.17.598.743 E srv  llama_server: exiting due to model loading error

- F2 mtp-n3-p0: [F2-mtp-n3-p0] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.682.129 I common_speculative_init_result: creating MTP draft context against the target model '/models/gguf/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf'
0.17.682.138 W llama_init_from_model: context type MTP requested but model doesn't contain MTP layers
0.17.682.138 E common_speculative_init_result: failed to create MTP context
0.17.682.144 E srv    load_model: failed to create MTP context
0.17.682.149 I srv    operator(): operator(): cleaning up before exit...
0.17.683.673 E srv  llama_server: exiting due to model loading error

- F2 mtp-n3-p06: [F2-mtp-n3-p06] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.696.329 I common_speculative_init_result: creating MTP draft context against the target model '/models/gguf/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf'
0.17.696.338 W llama_init_from_model: context type MTP requested but model doesn't contain MTP layers
0.17.696.338 E common_speculative_init_result: failed to create MTP context
0.17.696.344 E srv    load_model: failed to create MTP context
0.17.696.348 I srv    operator(): operator(): cleaning up before exit...
0.17.698.093 E srv  llama_server: exiting due to model loading error

- F2 mtp-n5-p06: [F2-mtp-n5-p06] el servidor murio al arrancar (rc=1); cola del log: pool init, n_threads = 16
0.17.699.125 I common_speculative_init_result: creating MTP draft context against the target model '/models/gguf/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf'
0.17.699.134 W llama_init_from_model: context type MTP requested but model doesn't contain MTP layers
0.17.699.134 E common_speculative_init_result: failed to create MTP context
0.17.699.140 E srv    load_model: failed to create MTP context
0.17.699.145 I srv    operator(): operator(): cleaning up before exit...
0.17.700.339 E srv  llama_server: exiting due to model loading error

- F2/ngram-simple: prompt_n real 2294 vs objetivo 3000 (>15%)
- F2/ngram-simple: prompt_n real 2294 vs objetivo 3000 (>15%)
- F5-prod/prod-final: prompt_n real 2294 vs objetivo 3000 (>15%)
- F5-prod/prod-final: prompt_n real 2294 vs objetivo 3000 (>15%)
- F5-prod/prod-final: prompt_n real 2294 vs objetivo 3000 (>15%)
