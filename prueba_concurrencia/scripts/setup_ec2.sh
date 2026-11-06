#!/usr/bin/env bash
# Prepara una instancia Ubuntu 24.04 para correr la prueba. Se corre una vez, desde la carpeta
# prueba_concurrencia: bash scripts/setup_ec2.sh
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update -q
# build-essential por si alguna dependencia no trae paquete armado para ARM (Graviton).
sudo apt-get install -y -q postgresql curl openssl build-essential

if ! command -v uv > /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv sync

# Muchas conexiones a la vez: archivos abiertos, cola de conexiones y puertos efímeros.
printf '* soft nofile 65535\n* hard nofile 65535\n' | sudo tee /etc/security/limits.d/concurrencia.conf > /dev/null
sudo sysctl -q -w net.core.somaxconn=8192 net.ipv4.tcp_max_syn_backlog=8192 \
    net.ipv4.ip_local_port_range="1024 65000" net.ipv4.tcp_tw_reuse=1

# PostgreSQL local para el escenario 8.
sudo -u postgres psql -qc "CREATE USER concurrencia WITH PASSWORD 'concurrencia'" 2> /dev/null || true
sudo -u postgres psql -qc "CREATE DATABASE concurrencia OWNER concurrencia" 2> /dev/null || true
DSN=postgresql://concurrencia:concurrencia@127.0.0.1/concurrencia
grep -q CONCURRENCIA_DSN ~/.bashrc || echo "export CONCURRENCIA_DSN=$DSN" >> ~/.bashrc

# Datos de la instancia para el informe (IMDSv2, solo accesible desde la propia instancia).
token=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
tipo=$(curl -s -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-type || echo desconocido)
mkdir -p results
uv run python - "$tipo" > results/instancia.json <<'EOF'
import json, os, platform, sys
import aiohttp, psutil
print(json.dumps({
    "tipo": sys.argv[1],
    "vcpu": os.cpu_count(),
    "memoria_mb": round(psutil.virtual_memory().total / 2**20),
    "sistema": platform.platform(),
    "python": platform.python_version(),
    "aiohttp": aiohttp.__version__,
}, indent=2))
EOF
cat results/instancia.json

sh scripts/certificado.sh certs
uv run pytest -q
echo
echo "Listo. Abrí una sesión nueva (o 'source ~/.bashrc') y corré: ./run_all.sh"
