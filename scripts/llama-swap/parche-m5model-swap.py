import sys
p = sys.argv[1]; s = open(p).read()
rep = [
 # backends detras de llama-swap: loopback, sin clave
 ('qwen_health() { curl -s -o /dev/null -w \'%{http_code}\' -m 5 -H "Authorization: Bearer $(key)" http://127.0.0.1:8080/health; }',
  'qwen_health() { curl -s -o /dev/null -w \'%{http_code}\' -m 5 http://127.0.0.1:18080/health; }  # H-058: tras llama-swap, sin clave'),
 ('http://127.0.0.1:8085/health; }  # Gufo exige clave tambien en /health',
  'http://127.0.0.1:18085/health; }  # H-058: Gufo DeepSeek tras llama-swap\nstrata_health() { curl -s -o /dev/null -w "%{http_code}" -m 5 http://127.0.0.1:8085/health; }  # Strata (reserva) sigue en :8085 con su clave'),
 ("    ss -lnt 2>/dev/null | grep -q ':8080 ' || return 0", "    ss -lnt 2>/dev/null | grep -q ':18080 ' || return 0"),
 ('echo "AVISO: el puerto 8080 sigue ocupado tras parar qwen"', 'echo "AVISO: el puerto 18080 sigue ocupado tras parar qwen"'),
 ('  [ -z "$pids" ] && [ "$(ds_health)" != "200" ] && return 0', '  [ -z "$pids" ] && [ "$(strata_health)" != "200" ] && return 0'),
 ('       && [ "$(ds_health)" != "200" ]; then', '       && [ "$(strata_health)" != "200" ]; then'),
 ("  for _ in $(seq 1 20); do ss -lnt 2>/dev/null | grep -q ':8085 ' || return 0; sleep 1; done\n  echo \"FALLO: el puerto 8085 sigue ocupado tras parar llama-ds41\"",
  "  for _ in $(seq 1 20); do ss -lnt 2>/dev/null | grep -q ':18085 ' || return 0; sleep 1; done\n  echo \"FALLO: el puerto 18085 sigue ocupado tras parar llama-ds41\""),
 ('  [ "$(ds_health)" = "200" ] && return 0\n  cd "$STRATA_DIR"', '  [ "$(strata_health)" = "200" ] && return 0\n  cd "$STRATA_DIR"'),
 ('    [ "$(ds_health)" = "200" ] && return 0\n    sleep 2', '    [ "$(strata_health)" = "200" ] && return 0\n    sleep 2'),
 ("modelo_8080() { curl -s -m 5 -H \"Authorization: Bearer $(key)\" http://127.0.0.1:8080/v1/models", "modelo_8080() { curl -s -m 5 http://127.0.0.1:18080/v1/models"),
 ('  echo "qwen     (:8080, Gufo)    -> $(qwen_health)  modelo: $(modelo_8080)"',
  '  echo "qwen     (:18080 tras llama-swap) -> $(qwen_health)  modelo: $(modelo_8080)"'),
 ('  if systemctl is-active -q llama-ds41; then m="Gufo IQ1_M"; elif [ -n "$(strata_pids)" ]; then m="Strata"; else m="-"; fi\n  echo "deepseek (:8085, $m) -> $(ds_health)"',
  '  echo "deepseek (:18085 Gufo IQ1_M) -> $(ds_health)   strata (:8085) -> $(strata_health)"\n  echo "llama-swap (:8080) -> $(systemctl is-active llama-swap)  cargados: $(curl -s -m 5 -H "Authorization: Bearer $(key)" http://127.0.0.1:8080/running | python3 -c \'import sys,json;print(" ".join(r["model"]+"="+r["state"] for r in json.load(sys.stdin).get("running",[])) or "-")\' 2>/dev/null)"'),
]
for a, b in rep:
    assert s.count(a) == 1, a[:70]
    s = s.replace(a, b)
assert 'ds_health)" = "200" ] && return 0\n    systemctl is-failed -q llama-ds41' in s  # start_ds41 sigue con ds_health
s = s.replace('# Uso:\n', '# H-058 (03-nov-2026): llama-swap atiende :8080 y llama a este script; los motores\n# escuchan en loopback sin clave (:18080 qwen/sincensura, :18085 DeepSeek-Gufo).\n#\n# Uso:\n', 1)
open(p, 'w').write(s); print('parche OK')
