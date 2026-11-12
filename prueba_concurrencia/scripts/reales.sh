#!/usr/bin/env bash
# Prueba contra repositorios reales (GUIA_REALES_SP.md, pedida por el usuario en nov 2026). Corre en la
# candidata (t4g.large en São Paulo), sola: PostgreSQL y el motor en Docker Compose, sin servidor simulado.
# El motor le pide a los repositorios de la muestra de LA Referencia (relevamiento_exploratorio/muestra/):
# un pedido a la vez por repositorio y 2 s entre pedidos; cada paso del barrido baja ítems distintos.
#
# Uso:
#   S3_BUCKET=… CONTACTO=… scripts/reales.sh              r25 r50 r75 r120 (~11 min cada uno)
#   S3_BUCKET=… CONTACTO=… scripts/reales.sh r75          solo un paso
# Variables:
#   CONTACTO                  correo para el User-Agent (obligatorio: los repositorios tienen que poder avisar)
#   S3_BUCKET                 bucket de prueba con borrado al día (obligatorio: no hay S3 falso)
#   DURACION, CALENTAMIENTO   acortan cada paso (ensayos)
#   RED_HOST=1                el motor usa la red del host (docker/compose.red-host.yaml); resultados <paso>-host
#   CARGA_WEB=1               el contenedor que ocupa el lugar de la API y el frontend (~450 MB, ~10 % de CPU)
# Deja los resultados en results/ (con <paso>-pedidos.csv, docker_stats.csv e instancia.json) y el informe en report.md.
set -euo pipefail
cd "$(dirname "$0")/.."

IDS=${*:-r25 r50 r75 r120}
# El compose pide IP_SERVIDOR (lo usan los escenarios del simulador); en el modo muestra no se usa.
export IP_SERVIDOR=127.0.0.1
export USUARIO="$(id -u):$(id -g)"
COMPOSE=(docker compose -f docker/compose.candidata.yaml)
SUFIJO=""
if [ "${RED_HOST:-0}" = 1 ]; then
    COMPOSE+=(-f docker/compose.red-host.yaml)
    SUFIJO="-host"
    echo "Red del host para el motor (sin el puente de Docker): resultados como <paso>$SUFIJO."
fi

if [ -z "${CONTACTO:-}" ]; then
    echo "!! Falta CONTACTO: el correo que va en el User-Agent de los pedidos a los repositorios."
    exit 1
fi
if [ -z "${S3_BUCKET:-}" ]; then
    echo "!! Falta S3_BUCKET: el bucket de prueba (con borrado al día). Contra repositorios reales no hay S3 falso."
    exit 1
fi
MUESTRA=../relevamiento_exploratorio/muestra
for archivo in repositorios.csv items.csv archivos.csv; do
    if [ ! -f "$MUESTRA/$archivo" ]; then
        echo "!! Falta $MUESTRA/$archivo: la rama tiene que incluir la muestra (relevamiento muestra-resumir)."
        exit 1
    fi
done
echo "Muestra: $(($(wc -l < "$MUESTRA/items.csv") - 1)) ítems de $(grep -c ',ok,' "$MUESTRA/repositorios.csv") repositorios útiles."
echo "Subida a S3 real: s3://$S3_BUCKET/${S3_PREFIJO:-prueba-concurrencia}/   User-Agent con contacto $CONTACTO"
mkdir -p results spool certs
if [ ! -w results ] || [ ! -w spool ]; then
    sudo chown -R "$(id -u):$(id -g)" results spool
fi
if ls results/*.json 2>/dev/null | grep -qv -e '/r[0-9]' -e instancia.json; then
    echo "!! results/ ya tiene resultados de otros escenarios: movelos antes, así el informe es solo de los reales."
    exit 1
fi

"${COMPOSE[@]}" build -q motor
"${COMPOSE[@]}" up -d --wait postgres
if [ "${CARGA_WEB:-0}" = 1 ]; then
    "${COMPOSE[@]}" --profile web up -d web-simulada
    echo "Carga de la API y el frontend: ${MEMORIA_WEB_MB:-450} MB y ${CPU_WEB:-0.1} de un núcleo."
fi

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
  "repositorios": "reales: muestra de LA Referencia",
  "s3": "s3://$S3_BUCKET",
  "carga_web": "$([ "${CARGA_WEB:-0}" = 1 ] && echo "${MEMORIA_WEB_MB:-450} MB, ${CPU_WEB:-0.1} núcleo" || echo "no")"
}
EOF

# Memoria y CPU por contenedor cada 5 s.
[ -f results/docker_stats.csv ] || echo "t,contenedor,cpu,memoria" > results/docker_stats.csv
(
    set +e +o pipefail
    while true; do
        ahora=$(date +%s)
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
    echo "== paso $id$SUFIJO"
    inicio=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    "${COMPOSE[@]}" run --rm -e CONTACTO motor motor --escenario "$id" --salida "results/$id$SUFIJO" \
        ${extra[@]+"${extra[@]}"} || echo "!! el paso $id falló"
    echo "$id$SUFIJO,$inicio,$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> results/tiempos.csv
    sleep 3
done

kill "$stats" 2>/dev/null || true
if [ "${CARGA_WEB:-0}" = 1 ]; then
    "${COMPOSE[@]}" --profile web stop web-simulada > /dev/null
fi
"${COMPOSE[@]}" run --rm --no-deps motor informe --resultados results --salida results/report.md
mv results/report.md report.md
echo
echo "Listo: report.md y results/. PostgreSQL sigue corriendo; para bajarlo: docker compose -f docker/compose.candidata.yaml down -v"
