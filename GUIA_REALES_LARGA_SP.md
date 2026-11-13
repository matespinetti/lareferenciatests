# Guía: corrida larga contra repositorios reales (São Paulo)

Una hora con **75 corrutinas** contra la muestra de LA Referencia, en la t4g.large de São Paulo. Complementa el barrido de `GUIA_REALES_SP.md`: aquellos pasos de 10 minutos se quedaban sin trabajo a los ~3 minutos. Esta corrida responde **"¿y sostenido?"**:

- si los ~60–70 MB/s se mantienen cuando se termina la ráfaga de red de la t4g (la red base es 64 MB/s);
- si los repositorios empiezan a bloquear con el tiempo (403, 429/503 o abandonos que suben hora a hora);
- si la CPU, la memoria, la subida a S3 y la base se mantienen estables durante la hora.

**Cómo alcanza la muestra para una hora.** Corre todos los ítems juntos (sin repartirlos en pasos), pero eso da solo ~15 minutos a carga plena. Por eso, un repositorio que termina sus ítems **vuelve a empezar**. Cada vuelta dura al menos 10 minutos, así que ningún archivo se pide más seguido que eso. La cortesía es la de siempre: un pedido a la vez por repositorio, 2 s entre pedidos, `Retry-After` y abandono ante dos 403, un `Retry-After` de más de 5 minutos o tres errores de red.

Las vueltas pueden salir más rápido por la caché del repositorio o de su CDN. El informe compara la **primera vuelta con las siguientes**: si la velocidad por conexión es parecida, vale toda la hora; si las siguientes son claramente más rápidas, vale la primera vuelta.

**Duración:** ~65 minutos de corrida, más ~10 de preparación. **Costo:** USD 1–1,5 en total:
- la t4g.large, ~USD 0,11/h por ~1,5 h;
- S3, ~USD 1: ~200 GB subidos que la regla de 1 día borra, y los PUT.

**Lo que ya existe y se reusa:** el bucket con la regla de 1 día, el rol IAM `prueba-concurrencia-s3`, el security group y el key pair `pruebareferencia-sp`. Solo hay que crear la instancia de nuevo.

---

## 0. Código (en tu Mac)

La rama `subida-s3` tiene que tener el escenario `r75h`:

```bash
cd ~/dev/lareferenciatests && git log --oneline -1 origin/subida-s3      # el commit de la corrida larga
```

## 1. AWS, en **São Paulo (sa-east-1)**

1. **Security group:** EC2 → Security Groups → el de la prueba anterior → Inbound rules. Si tu IP cambió desde la última vez, editá la regla SSH (22) y volvé a elegir **My IP**.
2. **Bucket:** S3 → el bucket de prueba → Management. Tiene que seguir la regla de ciclo de vida: borrar a 1 día y abortar los multipart incompletos.
3. **Instancia:** EC2 → Launch instance, igual que la anterior:

| Campo | Valor |
|---|---|
| Name | `candidata-sp` |
| AMI | Ubuntu Server 24.04 LTS, **64-bit (Arm)** |
| Instance type | **t4g.large** |
| Key pair | `pruebareferencia-sp` (el que ya tenés) |
| Network | VPC por defecto, IP pública. **Select existing security group:** el de la prueba anterior |
| Storage | 20 GiB gp3 |
| Advanced details → IAM instance profile | `prueba-concurrencia-s3` |
| Advanced details → Metadata response hop limit | **2** |
| Advanced details → Credit specification | **Unlimited** |

En tu Mac, con la IP pública nueva:

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/pruebareferencia-sp.pem
IP_B=…        # la pública de la instancia nueva
```

## 2. B: preparar

```bash
ssh -i "$CLAVE" ubuntu@"$IP_B" "sudo apt-get update && sudo apt-get install -y git tmux htop docker.io docker-compose-v2 && sudo usermod -aG docker ubuntu"
ssh -i "$CLAVE" ubuntu@"$IP_B" "git clone -b subida-s3 https://github.com/matespinetti/lareferenciatests.git"
ssh -i "$CLAVE" ubuntu@"$IP_B"
```

Si el primer `ssh` dice `Host key verification failed` (la IP ya la usaba otra máquina), borrá la entrada vieja con `ssh-keygen -R "$IP_B"` y repetí.

En B:

```bash
cd ~/lareferenciatests/prueba_concurrencia
nproc; free -m                                                       # 2 y unos 7.800 MB
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml build motor
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml run --rm --no-deps motor \
  python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
```

El último comando tiene que mostrar un ARN con `prueba-concurrencia-s3`. Si dice `Unable to locate credentials`, falta el hop limit en 2: Actions → Instance settings → Modify instance metadata options.

**Ensayo (~2 minutos).** Un minuto de la corrida larga, para ver que sale a internet, que sube a S3 y que no hay violaciones de cortesía:

```bash
export S3_BUCKET=prueba-concurrencia-mateo-sp AWS_REGION=sa-east-1 INSTANCIA=t4g.large CONTACTO=<tu correo>   # el nombre de tu bucket
DURACION=60 CALENTAMIENTO=10 RED_HOST=1 bash scripts/reales.sh r75h
grep -A3 "Corrida larga" report.md | head; grep "Cortesía" -A3 report.md | head -5
rm -rf results report.md      # que el informe final sea solo de la corrida de 1 hora
```

Al terminar tiene que decir `0 violaciones de cortesía`, y el informe tiene que traer la sección "Corrida larga".

## 3. B: la corrida de 1 hora

```bash
tmux new -s larga
cd ~/lareferenciatests/prueba_concurrencia
export S3_BUCKET=prueba-concurrencia-mateo-sp AWS_REGION=sa-east-1 INSTANCIA=t4g.large CONTACTO=<tu correo>   # el nombre de tu bucket
RED_HOST=1 CARGA_WEB=1 bash scripts/reales.sh r75h 2>&1 | tee run.log
```

- **Si se corta el SSH,** la corrida sigue en tmux. Para volver: `ssh -i "$CLAVE" ubuntu@"$IP_B"` y `tmux attach -t larga`.
- **Mientras corre, el motor no imprime nada** hasta el final (61 minutos). Para ver que anda, desde otra ventana:

  ```bash
  ssh -i "$CLAVE" ubuntu@"$IP_B" "tail -3 ~/lareferenciatests/prueba_concurrencia/results/docker_stats.csv"
  ```

Al final imprime `… N pedidos a M repositorios, 0 violaciones de cortesía, K abandonados, errores {…}` y deja `report.md`.

## 4. Traer los resultados (en tu Mac)

```bash
mkdir -p ~/dev/lareferenciatests/prueba_concurrencia/resultados/sp-reales-r75h && cd ~/dev/lareferenciatests/prueba_concurrencia/resultados/sp-reales-r75h
R=lareferenciatests/prueba_concurrencia
scp -i "$CLAVE" -r ubuntu@"$IP_B":"$R/results" .
scp -i "$CLAVE" ubuntu@"$IP_B":"$R/report.md" ubuntu@"$IP_B":"$R/run.log" .
ls -la results/
```

Tiene que estar `results/r75h-host.csv`, `.json` y `-pedidos.csv`. El `-pedidos.csv` pesa unos 50–100 MB, porque trae un pedido por fila.

En `report.md`, la sección **"Corrida larga"** trae:
- **cada 5 minutos:** MB/s bajados y subidos, repositorios con trabajo, ocupación, CPU de la máquina, memoria libre, RSS del motor, y los 403, 429/503 y errores de red;
- **la primera vuelta contra las siguientes:** pedidos, % correctos y velocidad por conexión.

Con eso, Claude actualiza los informes.

## 5. Limpiar

**Solo terminar la instancia:** EC2 → `candidata-sp` → Instance state → **Terminate**. El disco se borra con ella.

El bucket, el rol, el security group y el key pair quedan. Ninguno cuesta nada mientras no se usa, y el bucket se vacía solo con la regla de 1 día.
