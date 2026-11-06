#!/usr/bin/env bash
# Corre los escenarios y deja los resultados en results/ y el informe en report.md.
#
# Uso:
#   ./run_all.sh              versión chica (recomendada), ~19 min:
#                             escenarios 1 2 4 6 2h 7 s250 ss250 a 1 min cada uno, y el barrido de lugares con la red
#                             de la instancia simulada
#   ./run_all.sh completa     todos los escenarios con la duración de la spec y los dos barridos, ~2 h
#   ./run_all.sh 1 4 b10      solo esos escenarios, con la duración de la spec
# Variables:
#   INSTANCIA                 instancia de EC2 que se simula (c6gn.medium): fija el tope de red y contra qué
#                             compara el informe. Opciones en src/concurrencia/config.py (INSTANCIAS)
#   SERVIDOR_PROCESOS         procesos del servidor simulado (en Linux: los núcleos que quedan libres, hasta 4)
#   DURACION, CALENTAMIENTO   cambian la duración de cada escenario (segundos)
#   CONCURRENCIA_DSN          PostgreSQL para el escenario 8 (sin ella, se saltea)
#   RED_MB_S                  cambia el tope de red de los escenarios del problema real (0: sin tope)
#   ANCHO_MB_S                MB/s que el servidor entrega por conexión (2: supuesto; medirlo con
#                             `relevamiento medir-velocidad`)
#   PUERTO                    puerto del servidor simulado (8443)
#   PUERTO_S3, SPOOL          S3 falso (9443) y spool en disco (spool/) de los escenarios con subida (s250, ss250)
set -euo pipefail
cd "$(dirname "$0")"

BARRIDO="b2 b5 b10 b25 b50 b100 b250 b500 b1000 b2400"
BARRIDO_SIN_TOPE="bs2 bs5 bs10 bs25 bs50 bs100 bs250 bs500 bs1000 bs2400"
PUERTO=${PUERTO:-8443}
export PUERTO_S3=${PUERTO_S3:-9443}
export INSTANCIA=${INSTANCIA:-c6gn.medium}

# En Linux, el motor va solo en el núcleo 0 (emula una instancia de 1 vCPU y su CPU no se mezcla
# con la del servidor) y el servidor simulado se reparte en los demás núcleos.
MOTOR_CPU=()
SERVIDOR_CPU=()
nucleos=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
if command -v taskset > /dev/null && [ "$nucleos" -ge 2 ]; then
    MOTOR_CPU=(taskset -c 0)
    SERVIDOR_CPU=(taskset -c "1-$((nucleos - 1))")
    procesos_por_defecto=$((nucleos - 1 < 4 ? nucleos - 1 : 4))
else
    procesos_por_defecto=1
fi
export SERVIDOR_PROCESOS=${SERVIDOR_PROCESOS:-$procesos_por_defecto}
echo "Instancia simulada: $INSTANCIA · servidor en $SERVIDOR_PROCESOS proceso(s) · núcleos: $nucleos"
if [ "$nucleos" -lt 3 ] && [ "$(uname)" = Linux ]; then
    echo "!! Con menos de 3 núcleos, servidor y motor compiten por la CPU: usá una instancia de 4 vCPU."
fi

ulimit -n 65535 2>/dev/null || ulimit -n 10240 2>/dev/null || true
echo "Límite de archivos abiertos: $(ulimit -n)"

sh scripts/certificado.sh certs
uv sync -q
mkdir -p results
[ -f results/tiempos.csv ] || echo "escenario,inicio_utc,fin_utc" > results/tiempos.csv

# correr_ids "<ids>" <duración> <calentamiento>: vacíos usan la duración de cada escenario.
correr_ids() {
    local ids=$1 duracion=${2:-} calentamiento=${3:-}
    local extra=()
    if [ -n "$duracion" ]; then extra+=(--duracion "$duracion"); fi
    if [ -n "$calentamiento" ]; then extra+=(--calentamiento "$calentamiento"); fi
    for id in $ids; do
        if [ "$id" = 8 ] && [ -z "${CONCURRENCIA_DSN:-}" ]; then
            echo "== escenario 8: falta CONCURRENCIA_DSN, se saltea"
            continue
        fi
        echo "== escenario $id"
        # Con subida (s250, ss250): el subidor va solo en el núcleo 1, y el servidor y el S3 falso en los demás.
        local servidor_cpu=(${SERVIDOR_CPU[@]+"${SERVIDOR_CPU[@]}"}) s3=""
        if [[ "$id" =~ ^s+[0-9]+$ ]]; then
            if [ ${#MOTOR_CPU[@]} -gt 0 ] && [ "$nucleos" -ge 4 ]; then
                servidor_cpu=(taskset -c "2-$((nucleos - 1))")
                export SUBIDOR_CPU=1
            fi
            ${servidor_cpu[@]+"${servidor_cpu[@]}"} .venv/bin/s3falso --puerto "$PUERTO_S3" --certs certs &
            s3=$!
        fi
        ${servidor_cpu[@]+"${servidor_cpu[@]}"} .venv/bin/servidor --escenario "$id" --puerto "$PUERTO" --certs certs &
        local servidor=$!
        local ultimo=$((PUERTO + SERVIDOR_PROCESOS - 1))
        for _ in $(seq 100); do
            if curl -s --cacert certs/ca.pem --resolve "control.sim.localhost:$ultimo:127.0.0.1" \
                "https://control.sim.localhost:$ultimo/__control/estado" > /dev/null; then
                break
            fi
            sleep 0.2
        done
        if [ -n "$s3" ]; then
            for _ in $(seq 100); do
                if curl -s --cacert certs/ca.pem --resolve "s3.sim.localhost:$PUERTO_S3:127.0.0.1" \
                    "https://s3.sim.localhost:$PUERTO_S3/__s3/estado" > /dev/null; then
                    break
                fi
                sleep 0.2
            done
        fi
        local inicio
        inicio=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        ${MOTOR_CPU[@]+"${MOTOR_CPU[@]}"} .venv/bin/motor --escenario "$id" --salida "results/$id" --puerto "$PUERTO" --certs certs \
            ${extra[@]+"${extra[@]}"} || echo "!! el escenario $id falló"
        echo "$id,$inicio,$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> results/tiempos.csv
        kill "$servidor" ${s3:+"$s3"} 2>/dev/null || true
        wait "$servidor" ${s3:+"$s3"} 2>/dev/null || true
        unset SUBIDOR_CPU
        sleep 3
    done
}

case "${1:-chica}" in
    chica)
        correr_ids "1 2 4 6 2h 7 s250 ss250" "${DURACION:-60}" "${CALENTAMIENTO:-10}"
        correr_ids "$BARRIDO" "${DURACION:-}" "${CALENTAMIENTO:-}"
        ;;
    completa)
        correr_ids "1 2 3 4 5 6 7 8 9 1h 2h 3h s250 ss250 $BARRIDO $BARRIDO_SIN_TOPE" "${DURACION:-}" "${CALENTAMIENTO:-}"
        ;;
    *)
        correr_ids "$*" "${DURACION:-}" "${CALENTAMIENTO:-}"
        ;;
esac

.venv/bin/informe --resultados results --salida report.md
echo
echo "Listo: report.md. Con results/tiempos.csv buscá en CloudWatch el CPUCreditBalance de cada escenario."
