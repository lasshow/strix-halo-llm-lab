# Servir el modelo 24/7 con systemd

## Unidad

```ini
[Unit]
Description=llama.cpp server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=llama
EnvironmentFile=/etc/llama-server/server.env
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
  --api-key ${LLAMA_API_KEY} \
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
| `--batch 4096 / --ubatch 2048` | **Medido**, no copiado. Ver [`metodologia.md`](metodologia.md). El barrido dio un +1% marginal de 2048 a 4096, y 4096 agrava el consumo de memoria: 2048 es la eleccion prudente. |
| `--lazy-mode off` | **+92% de prefill** en iGPU. Ver [`carga-diferida-y-oom.md`](carga-diferida-y-oom.md). |
| `OOMScoreAdjust=500` | Que muera el modelo, nunca la maquina. Ver abajo. |
| `--api-key` | Vía `EnvironmentFile`, nunca escrita en la unidad. |
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

```bash
sudo install -d -m 750 /etc/llama-server
printf 'LLAMA_API_KEY=%s\n' "$(openssl rand -base64 32)" | sudo tee /etc/llama-server/server.env >/dev/null
sudo chmod 600 /etc/llama-server/server.env
```

Nunca poner la clave en la unidad systemd: `systemctl show` la expondría a cualquier usuario.

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
