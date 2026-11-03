#!/bin/bash
# Despliegue H-058 en el M5 (como root via sudo). Deja backups con sufijo .bak-h058.
set -eu
M5_USER=${M5_USER:?exporta M5_USER (usuario que corre llama-swap y los contenedores rootless)}
B=.bak-h058
cd /tmp/m5swap
# 1) backups
cp -a /usr/local/bin/m5-model /usr/local/bin/m5-model$B
cp -a /etc/systemd/system/llama-swap.service /etc/systemd/system/llama-swap.service$B
cp -a /etc/systemd/system/llama-uncensored.service /etc/systemd/system/llama-uncensored.service$B
cp -a /etc/systemd/system/llama-ds41.service /etc/systemd/system/llama-ds41.service$B
cp -a /models/llama-swap/config.yaml /models/llama-swap/config.yaml$B
cp -a /models/llama-swap/llama-swap /models/llama-swap/llama-swap.v268
# 2) scripts
install -m 755 m5-ocupado m5-swap-run m5-swap-stop /usr/local/bin/
install -m 755 m5-model.nuevo /usr/local/bin/m5-model
# 3) motores a loopback sin clave (la clave la pone llama-swap)
D=/etc/systemd/system/llama-flashnext.service.d
python3 - <<'PY'
import re
src = open('/etc/systemd/system/llama-flashnext.service.d/60-gufo.conf').read()
i = src.index('ExecStart=/bin/bash'); j = src.index('ExecStop=')
ex = src[i:j]
assert ex.count('-p 0.0.0.0:8080:8080') == 1 and ex.count('--api-key "$$CLAVE" ') == 1
ex = ex.replace('-p 0.0.0.0:8080:8080', '-p 127.0.0.1:18080:8080').replace('--api-key "$$CLAVE" ', '')
open('/etc/systemd/system/llama-flashnext.service.d/70-tras-llama-swap.conf', 'w').write(
  '# H-058: Gufo/Qwen escucha solo en loopback :18080 y sin clave; la puerta publica\n'
  '# :8080 (con clave) es llama-swap. Quitar este fichero = volver al modo anterior.\n'
  '[Service]\nExecStart=\n' + ex)
for f, a, b in [('/etc/systemd/system/llama-uncensored.service', '-p 0.0.0.0:8080:8080', '-p 127.0.0.1:18080:8080'),
                ('/etc/systemd/system/llama-ds41.service', '-p 0.0.0.0:8085:8080', '-p 127.0.0.1:18085:8080')]:
    s = open(f).read()
    assert s.count(a) == 1 and s.count('--api-key "$$CLAVE"') == 1, f
    s = s.replace(a, b).replace(' --api-key "$$CLAVE"', '')
    s = s.replace('[Service]', '# H-058: tras llama-swap (loopback, sin clave)\n[Service]', 1)
    open(f, 'w').write(s)
print('unidades OK')
PY
# 4) claves para llama-swap
umask 077
python3 - <<'PY'
k = next(l.strip().replace(' ', '') for l in open('/etc/llama-server/api-keys.txt') if l.strip() and not l.strip().startswith('#'))
d = open('/etc/llama-server/ds41.key').read().strip()
open('/etc/llama-server/llama-swap.env', 'w').write(f'M5_KEY_QWEN={k}\nM5_KEY_DS={d}\n')
PY
chown "$M5_USER:$M5_USER" /etc/llama-server/llama-swap.env; chmod 600 /etc/llama-server/llama-swap.env
# 5) llama-swap v269 + config + unidad
install -m 755 llama-swap /models/llama-swap/llama-swap
install -m 644 -o "$M5_USER" -g "$M5_USER" config.yaml /models/llama-swap/config.yaml
sed "s/__M5_USER__/$M5_USER/; s/__UID__/$(id -u "$M5_USER")/" llama-swap.service > /etc/systemd/system/llama-swap.service
systemctl daemon-reload
echo DESPLEGADO
