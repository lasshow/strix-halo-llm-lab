# Servir el modelo 24/7 con systemd

## Unidad

```ini
[Unit]
Description=llama.cpp server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# El usuario del servicio debe ser el DUEÑO del fichero de la clave y tener
# acceso al directorio (ver "Secreto de API"). En este laboratorio es `lasso`;
# si se usa un usuario dedicado, hay que cambiar el chown de esa sección.
User=lasso
ExecStart=/opt/llama.cpp/build/bin/llama-server \
  --model /models/gguf/<modelo>/<modelo>-00001-of-0000N.gguf \
  --mmproj /models/gguf/<modelo>/mmproj-F16.gguf \
  --alias mi-modelo \
  -ngl 99 \
  -c 262144 \
  -np 2 \
  -fa on \
  -kvu \
  --cache-reuse 256 \
  --cache-ram 4096 \
  --batch-size 4096 \
  --ubatch-size 2048 \
  --no-context-shift \
  --lazy-mode off \
  --threads 16 \
  --host 0.0.0.0 --port 8080 \
  --api-key-file /etc/llama-server/api-keys.txt \
  --metrics
Restart=on-failure
RestartSec=10
# NUNCA negativo: ver "El OOM que mata la maquina" mas abajo
OOMScoreAdjust=500

[Install]
WantedBy=multi-user.target
```

### Notas sobre los parámetros

| Flag | Por qué |
|---|---|
| `-ngl 99` | Todas las capas a la iGPU. Con memoria unificada no hay motivo para dejar capas en CPU. |
| `--threads 16` | Núcleos físicos, no hilos lógicos. Poner 32 no mejora y compite consigo mismo. |
| `-np 2` | Dos slots concurrentes. |
| `-kvu` | KV unificada: los slots comparten el pool en vez de partirlo. |
| `-fa on` | Flash attention. |
| `--batch 4096 / --ubatch 2048` | **Medido**, no copiado. Ver [`metodologia.md`](metodologia.md). Con `--lazy-mode off`, `--ubatch 4096` **no arranca**: OOM al cargar y bucle de reintentos de systemd. Entre 1024 y 2048 **no hay diferencia medible** con `batch` fijo a 4096 (H-022); se usa 2048 por continuidad, y bajar a 1024 no cuesta rendimiento si necesitas margen de memoria. El `+2,8%` que se publicó a favor de 2048 era un artefacto del barrido viejo. |
| `--lazy-mode off` | **+16% de prefill** contra el servidor real (el banco `llama-bench` dice +92%, exagera). Contrapartida: los pesos dejan de ser reclamables, lo que hace inviable `--ubatch 4096`. Ver [`carga-diferida-y-oom.md`](carga-diferida-y-oom.md). |
| `OOMScoreAdjust=500` | Que muera el modelo, nunca la maquina. Ver abajo. |
| `--api-key-file` | La clave vive en `/etc/llama-server/api-keys.txt` (`600 lasso:lasso`, dentro de un directorio `750 root:lasso`). **No se pasa por `argv`**: ahí sería legible por cualquier usuario local vía `/proc/<pid>/cmdline` (H-021). El directorio debe ser accesible por el usuario del servicio, no solo el fichero. |
| `--metrics` | Expone `/metrics` para Prometheus. |

## ⚠️ La trampa de SELinux

En Fedora con SELinux en *Enforcing*, un binario que vive fuera de `/usr` **arranca perfectamente a mano** pero falla como servicio:

```
llama-server.service: Failed to execute command: Permission denied
llama-server.service: Failed at step EXEC spawning ...: Permission denied
status=203/EXEC
```

Los permisos del fichero son correctos. El problema es la **etiqueta** SELinux. Solución:

```bash
sudo semanage fcontext -a -t bin_t "/opt/llama.cpp/build/bin(/.*)?"
sudo restorecon -RF /opt/llama.cpp/build/bin
sudo systemctl restart llama-server
```

Diagnóstico rápido: `ls -Z` sobre el binario, y `sudo ausearch -m avc -ts recent`.

## ⚠️ El OOM que mata la maquina entera

`OOMScoreAdjust` **negativo** en un servicio que ocupa el 80% de la RAM es una trampa
mortal. Le dice al kernel "mata lo que sea antes que a este proceso", y el kernel obedece:
va matando `sshd`, `NetworkManager`, `systemd-resolved`, `tailscaled`... Sintoma tipico:

- el equipo **responde al ping** (el kernel vive)
- pero **SSH rechaza la conexion** (no queda espacio de usuario)
- y **no se recupera solo**: exige reinicio fisico

Un servidor de inferencia debe ser lo **primero** en morir, no lo ultimo: si muere el
servicio, systemd lo reinicia; si muere la maquina, hay que ir a pulsar el boton.
Historia completa en [`carga-diferida-y-oom.md`](carga-diferida-y-oom.md).

## Secreto de API

La unidad usa **un solo mecanismo**: `--api-key-file` apuntando a un fichero con
la clave *en crudo* (una por línea). No hay `EnvironmentFile` ni variable
`LLAMA_API_KEY` en el servicio.

Esto es una corrección: versiones anteriores de esta guía creaban un
`server.env` con `LLAMA_API_KEY=...` y a la vez documentaban `--api-key-file`.
Eran dos mecanismos mezclados y el fichero de entorno no lo leía nadie. Formato
distinto además: `--api-key-file` espera la clave sola, **sin** el prefijo
`LLAMA_API_KEY=`.

```bash
# el directorio debe ser accesible por el usuario del servicio (aquí, lasso)
sudo install -d -m 750 -o root -g lasso /etc/llama-server
openssl rand -base64 32 | sudo tee /etc/llama-server/api-keys.txt >/dev/null
sudo chown lasso:lasso /etc/llama-server/api-keys.txt
sudo chmod 600 /etc/llama-server/api-keys.txt
```

Comprobación de que quedó como debe:

```bash
sudo ls -l /etc/llama-server/     # api-keys.txt -> -rw------- lasso lasso
```

Nunca poner la clave en la unidad systemd (`systemctl show` la expondría) ni en
`argv` (visible en `/proc/<pid>/cmdline` para cualquier usuario local, H-021).

`LLAMA_API_KEY` sí existe, pero **solo del lado del cliente**: es la variable
que leen `smoke-test.sh` y los scripts de medición para autenticarse.

## Cortafuegos

Abrir el puerto **solo** a la red local, nunca a Internet:

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=<tu-red>/24 port port=8080 protocol=tcp accept'
sudo firewall-cmd --reload
```

`llama-server` no está pensado como servicio expuesto públicamente. Para acceso remoto, una VPN de malla por delante.

## Comprobaciones

```bash
systemctl is-active llama-server
curl -s -H "Authorization: Bearer $KEY" localhost:8080/v1/models
journalctl -u llama-server -n 50 --no-pager
```
