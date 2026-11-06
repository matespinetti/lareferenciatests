#!/usr/bin/env bash
# Validación en una instancia real (GUIA_DOS_MAQUINAS.md). Corre en la candidata (c6gn.medium):
# PostgreSQL y el motor en Docker Compose, contra el servidor simulado que corre en otra instancia.
#
# Uso:
#   S3_BUCKET=… scripts/candidata.sh <IP privada del servidor>   b120 s120 v120 (~4 min cada uno), subiendo a S3 real
#   scripts/candidata.sh 10.0.1.23 v120h                          la de 1 hora, contra el S3 falso del servidor
# Variables:
#   S3_BUCKET                 bucket de S3 real (con el rol de la instancia); sin él, la subida va al S3 falso
#                             del servidor (PUERTO_S3)
#   SERVIDOR_PROCESOS         procesos del servidor simulado (3, puertos 8443-8445)
#   DURACION, CALENTAMIENTO   acortan cada escenario (ensayos)
# Deja los resultados en results/ (con docker_stats.csv e instancia.json) y el informe en report.md.
set -euo pipefail
cd "$(dirname "$0")/.."

IP=${1:?uso: scripts/candidata.sh <IP privada del servidor> [escenarios...]}
shift
IDS=${*:-b120 s120 v120}
export IP_SERVIDOR=$IP
export SERVIDOR_PROCESOS=${SERVIDOR_PROCESOS:-3}
export USUARIO="$(id -u):$(id -g)"
COMPOSE=(docker compose -f docker/compose.candidata.yaml)
PUERTO=8443

if [ ! -f certs/ca.pem ]; then
    echo "!! Falta certs/ca.pem: copiá la del servidor simulado (GUIA_DOS_MAQUINAS.md)."
    exit 1
fi
if [ -n "${S3_BUCKET:-}" ]; then
    echo "Subida a S3 real: s3://$S3_BUCKET/${S3_PREFIJO:-prueba-concurrencia}/"
else
    echo "S3_BUCKET vacío: la subida va al S3 falso en $IP:${PUERTO_S3:-9443}."
fi
mkdir -p results spool

# El servidor tiene que responder en todos sus procesos antes de empezar.
if [[ "$IP" =~ ^[0-9.]+$ ]]; then
    for puerto in $(seq "$PUERTO" $((PUERTO + SERVIDOR_PROCESOS - 1))); do
        if ! curl -sf --max-time 5 --cacert certs/ca.pem --resolve "control.sim.localhost:$puerto:$IP" \
            "https://control.sim.localhost:$puerto/__control/estado" > /dev/null; then
            echo "!! El servidor simulado no responde en $IP:$puerto. ¿Está corriendo con HOST=0.0.0.0 y el security group abierto?"
            exit 1
        fi
    done
    echo "Servidor simulado en $IP:$PUERTO-$((PUERTO + SERVIDOR_PROCESOS - 1)): responde."
    if [ -z "${S3_BUCKET:-}" ] && ! curl -sf --max-time 5 --cacert certs/ca.pem \
        --resolve "s3.sim.localhost:${PUERTO_S3:-9443}:$IP" "https://s3.sim.localhost:${PUERTO_S3:-9443}/__s3/estado" > /dev/null; then
        echo "!! Sin S3_BUCKET la subida va al S3 falso, y no responde en $IP:${PUERTO_S3:-9443}."
        exit 1
    fi
fi

"${COMPOSE[@]}" build -q motor
"${COMPOSE[@]}" up -d --wait postgres

# Datos de la máquina (IMDSv2; fuera de EC2 quedan vacíos).
token=$(curl -s --max-time 2 -X PUT http://169.254.169.254/latest/api/token \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300" || true)
metadato() {
    curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/$1" || true
}
memoria_mb=$(awk '/MemTotal/ {printf "%d", $2 / 1024}' /proc/meminfo 2>/dev/null || echo 0)
cat > results/instancia.json <<EOF
{
  "tipo": "$(metadato instance-type)",
  "zona": "$(metadato placement/availability-zone)",
  "vcpu": $(nproc 2>/dev/null || echo 0),
  "memoria_mb": $memoria_mb,
  "kernel": "$(uname -r)",
  "docker": "$(docker version --format '{{.Server.Version}}' 2>/dev/null)",
  "servidor_simulado": "$IP, $SERVIDOR_PROCESOS procesos",
  "s3": "${S3_BUCKET:+s3://$S3_BUCKET}${S3_BUCKET:-S3 falso}"
}
EOF

# Memoria y CPU por contenedor cada 5 s (PostgreSQL incluido). Cuesta algo de CPU en 1 vCPU, por eso no más seguido.
[ -f results/docker_stats.csv ] || echo "t,contenedor,cpu,memoria" > results/docker_stats.csv
(
    while true; do
        ahora=$(date +%s)
        # Solo los contenedores de esta prueba (proyecto `candidata`).
        contenedores=$(docker ps -q --filter label=com.docker.compose.project=candidata)
        if [ -n "$contenedores" ]; then
            # shellcheck disable=SC2086
            docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' $contenedores \
                | sed "s|^|$ahora,|" >> results/docker_stats.csv
        fi
        sleep 5
    done
) &
stats=$!
trap 'kill $stats 2>/dev/null || true' EXIT

[ -f results/tiempos.csv ] || echo "escenario,inicio_utc,fin_utc" > results/tiempos.csv
extra=()
if [ -n "${DURACION:-}" ]; then extra+=(--duracion "$DURACION"); fi
if [ -n "${CALENTAMIENTO:-}" ]; then extra+=(--calentamiento "$CALENTAMIENTO"); fi
for id in $IDS; do
    echo "== escenario $id"
    inicio=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    "${COMPOSE[@]}" run --rm motor motor --escenario "$id" --salida "results/$id" ${extra[@]+"${extra[@]}"} \
        || echo "!! el escenario $id falló"
    echo "$id,$inicio,$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> results/tiempos.csv
    sleep 3
done

kill "$stats" 2>/dev/null || true
"${COMPOSE[@]}" run --rm --no-deps motor informe --resultados results --salida results/report.md
mv results/report.md report.md
echo
echo "Listo: report.md y results/. PostgreSQL sigue corriendo; para bajarlo: docker compose -f docker/compose.candidata.yaml down -v"
