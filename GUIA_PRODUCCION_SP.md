# Guía: prueba de producción en São Paulo con una t4g.large

La última prueba antes de decidir. La máquina candidata es una **t4g.large** (2 vCPU, 8 GB), con todo lo que llevaría en producción:
- el motor, con la red del host;
- el subidor;
- PostgreSQL escribiendo en lotes;
- un contenedor que ocupa el lugar de la API y el frontend (~450 MB y ~10 % de un núcleo), que todavía no existen.

Todo en **São Paulo (sa-east-1)**. Primero se mide la velocidad de los repositorios desde ahí, y el simulador la usa.

```
A: c6gn.large (simulador)                         B: t4g.large (la candidata)
   1. medir-velocidad desde São Paulo                Docker Compose:
   2. servidor simulado con esa velocidad  ◄────       motor (red del host) + subidor
      y la red base de la t4g.large (~61 MB/s)         PostgreSQL (lotes) + API/frontend simulados
                                                        └──► S3 real en São Paulo
```

Corre un barrido de corrutinas: **25, 50, 75 y 120**, de 3,5 minutos cada uno. Mide lo mismo que antes (MB/s, CPU de la máquina y de cada proceso, memoria por contenedor, disco, retraso del bucle, errores y cortesía), y además **los créditos de CPU** de la t4g contra su línea base.

**Duración:** ~1 hora en total. **Costo:** menos de USD 1 (c6gn.large USD 0,14/h y t4g.large USD 0,11/h en São Paulo; S3, centavos).

Abreviaturas: **`$IP_A`** y **`$IP_B`** son las IP públicas, y **`$IP_PRIVADA_A`** es la privada de A. Los pasos que se repiten con `GUIA_DOS_MAQUINAS.md` están explicados ahí en más detalle.

---

## 0. Código (en tu Mac)

```bash
cd ~/dev/lareferenciatests && git pull
```

Todo está en la rama `subida-s3`.

## 1. AWS, en la región **South America (São Paulo) sa-east-1**

Arriba a la derecha de la consola, elegí São Paulo. Los recursos de Virginia no se ven desde ahí.

1. **Bucket:** S3 → Create bucket.
   - Nombre: por ejemplo `prueba-concurrencia-mateo-sp`. Región: sa-east-1.
   - Regla de ciclo de vida: borrar a 1 día y abortar los multipart incompletos.
2. **Rol IAM:** IAM es global.
   - Si borraste `prueba-concurrencia-s3`, crealo de nuevo (paso 2 de `GUIA_DOS_MAQUINAS.md`).
   - En su política, cambiá el bucket por el nuevo: `arn:aws:s3:::prueba-concurrencia-mateo-sp/prueba-concurrencia/*`.
3. **Key pair:** los pares de claves son por región, así que creá uno nuevo en São Paulo (por ejemplo `pruebareferencia-sp`) y guardá el `.pem` en `prueba_concurrencia/claves/`.
4. **Instancia A, el simulador:**

| Campo | Valor |
|---|---|
| Name | `simulador-sp` |
| AMI | Ubuntu Server 24.04 LTS, **64-bit (Arm)** |
| Instance type | **c6gn.large** |
| Key pair | `pruebareferencia-sp` |
| Network | VPC por defecto, IP pública. Security group nuevo `prueba-concurrencia`, con SSH (22) desde My IP |
| Storage | 20 GiB gp3 |

5. **Instancia B, la candidata.** Lanzala después de A, porque va en su misma subred y su mismo security group:

| Campo | Valor |
|---|---|
| Name | `candidata-sp` |
| AMI | Ubuntu Server 24.04 LTS, **64-bit (Arm)** |
| Instance type | **t4g.large** |
| Key pair | `pruebareferencia-sp` |
| Network | **La misma subred que A** y el security group `prueba-concurrencia` |
| Storage | **50 GiB gp3**, como en producción |
| Advanced details → IAM instance profile | `prueba-concurrencia-s3` |
| Advanced details → Metadata response hop limit | **2** |
| Advanced details → Credit specification | **Unlimited**, el que viene por defecto: dejalo así |

6. **Security group:** dos reglas de entrada más, Custom TCP en los puertos **8443-8444**, con origen el mismo security group.

En tu Mac:

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/pruebareferencia-sp.pem
chmod 400 "$CLAVE"
IP_A=…        # pública del simulador
IP_B=…        # pública de la candidata
```

## 2. A: preparar y medir la velocidad desde São Paulo

```bash
ssh -i "$CLAVE" ubuntu@"$IP_A"
```

Adentro:

```bash
sudo apt-get update && sudo apt-get install -y git tmux htop
git clone -b subida-s3 https://github.com/matespinetti/lareferenciatests.git
cd lareferenciatests/prueba_concurrencia && bash scripts/setup_ec2.sh
exit
```

Volvé a entrar, para que tome el límite de archivos abiertos. Después medí la velocidad. Hace pedidos a repositorios reales con la cortesía estricta de siempre (30 s entre pedidos a un host, 2 a la vez) y tarda unos 2 minutos:

```bash
tmux new -s sp
cd ~/lareferenciatests/relevamiento_exploratorio && uv sync
uv run relevamiento medir-velocidad --contacto TU_CORREO --en-horario-ok --salida medicion-sp
uv run relevamiento verificar-cortesia --salida medicion-sp --medicion        # tiene que decir OK
python3 -c "import json; d=json.load(open('medicion-sp/velocidad.json'))['mb_s_por_repositorio']; print(f'export ANCHO_MB_S={d[\"p50\"]} ANCHO_P25={d[\"p25\"]} ANCHO_P75={d[\"p75\"]}')"
```

La última línea imprime algo como `export ANCHO_MB_S=3.1 ANCHO_P25=1.2 ANCHO_P75=6.4`. **Copiala:** va igual en A y en B, para que el simulador y el informe usen la misma velocidad.

## 3. A: levantar el simulador con esa velocidad

Pegá el `export` del paso anterior y después:

```bash
cd ~/lareferenciatests/prueba_concurrencia
ulimit -n 65535
HOST=0.0.0.0 INSTANCIA=t4g.large SERVIDOR_PROCESOS=2 .venv/bin/servidor --escenario t50 --certs certs
```

Tiene que decir `… red de t4g.large: 61.0 MB/s, 2 proceso(s)`. Ese tope es la **red base de la t4g.large**: la ráfaga de red de la t4g dura 5–60 minutos, y en una corrida corta engañaría.

Dejalo corriendo. En otra terminal de la Mac, copiá la CA a B:

```bash
scp -i "$CLAVE" ubuntu@"$IP_A":~/lareferenciatests/prueba_concurrencia/certs/ca.pem /tmp/ca-prueba.pem
```

## 4. B: preparar la candidata

```bash
ssh -i "$CLAVE" ubuntu@"$IP_B" "sudo apt-get update && sudo apt-get install -y git tmux htop docker.io docker-compose-v2 && sudo usermod -aG docker ubuntu"
ssh -i "$CLAVE" ubuntu@"$IP_B" "git clone -b subida-s3 https://github.com/matespinetti/lareferenciatests.git && mkdir -p lareferenciatests/prueba_concurrencia/certs lareferenciatests/prueba_concurrencia/results lareferenciatests/prueba_concurrencia/spool"
scp -i "$CLAVE" /tmp/ca-prueba.pem ubuntu@"$IP_B":~/lareferenciatests/prueba_concurrencia/certs/ca.pem
ssh -i "$CLAVE" ubuntu@"$IP_B"
```

En B:

```bash
echo "export IP_PRIVADA_A=…" >> ~/.bashrc && source ~/.bashrc      # la IP privada de A
cd ~/lareferenciatests/prueba_concurrencia
nproc; free -m                                                      # 2 y unos 7.800 MB
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml build motor
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml run --rm --no-deps motor \
  python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
```

El último comando tiene que mostrar un ARN con `prueba-concurrencia-s3`. Si dice `Unable to locate credentials`, falta poner el hop limit en 2.

## 5. B: correr el barrido

Pegá el mismo `export ANCHO_MB_S=… ANCHO_P25=… ANCHO_P75=…` del paso 2 y después:

```bash
tmux new -s candidata
cd ~/lareferenciatests/prueba_concurrencia
export S3_BUCKET=prueba-concurrencia-mateo-sp AWS_REGION=sa-east-1 INSTANCIA=t4g.large
RED_HOST=1 CARGA_WEB=1 bash scripts/candidata.sh "$IP_PRIVADA_A" t25 t50 t75 t120 2>&1 | tee run.log
```

Tarda ~15 minutos. En cada escenario tiene que aparecer `0 violaciones, errores {}`. Al arrancar tiene que decir `Carga de la API y el frontend: 450 MB…` y `Red del host…`.

En `report.md`, la sección **"Validación en una t4g.large real"** muestra:
- los MB/s y los MB/s por corrutina de cada barrido;
- la CPU de la máquina **contra su línea base (30 %)**, con el cargo extra estimado si se pasa;
- la memoria por contenedor: motor, PostgreSQL y API/frontend.

## 6. Traer los resultados (en tu Mac)

```bash
mkdir -p ~/dev/lareferenciatests/resultados-sp && cd ~/dev/lareferenciatests/resultados-sp
R=lareferenciatests/prueba_concurrencia
scp -i "$CLAVE" -r ubuntu@"$IP_B":"$R/results" .
scp -i "$CLAVE" ubuntu@"$IP_B":"$R/report.md" ubuntu@"$IP_B":"$R/run.log" .
scp -i "$CLAVE" -r ubuntu@"$IP_A":lareferenciatests/relevamiento_exploratorio/medicion-sp .
ls results/t50.* medicion-sp/velocidad.json
```

## 7. Limpiar (en São Paulo)

1. **EC2:** terminar `candidata-sp` y `simulador-sp`.
2. **S3:** vaciar y borrar el bucket.
3. **Security group:** borrar `prueba-concurrencia`, cuando las instancias terminen de apagarse.
4. **Key pair:** `pruebareferencia-sp` se puede borrar. El rol IAM no cuesta nada si queda.
