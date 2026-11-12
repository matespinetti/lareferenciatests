# Guía: prueba contra repositorios reales en São Paulo

Repite el barrido de corrutinas de la t4g.large, pero **contra repositorios reales**: la muestra de LA Referencia, ya cosechada en `relevamiento_exploratorio/muestra/` (270 repositorios en 200 hosts). Responde a la objeción del profesor de que el simulador es falso.

Hace falta **una sola máquina**: la candidata, una t4g.large. No hay simulador ni CA de prueba.

```
B: t4g.large (São Paulo)                     repositorios de la muestra (Brasil, España, México, …)
   Docker Compose:                    ──►    un pedido a la vez por repositorio, 2 s entre pedidos
     motor (red del host) + subidor          (artículos OJS: página → PDF; DSpace: archivo directo)
     PostgreSQL (lotes) + API/frontend simulados
       └──► S3 real en São Paulo (bucket de prueba)
```

**El barrido:** **25, 50, 75 y 120 corrutinas**, en los pasos `r25`, `r50`, `r75` y `r120`.
- Cada paso dura 10 minutos, más 1 de calentamiento.
- Cada paso baja **ítems distintos** de cada repositorio, con un tope de 1 GB por repositorio.
- Cada paso tiene ~267 repositorios y ~16.000 ítems: ~5.200 con el archivo directo (~32 GB) y ~11.000 artículos OJS.

**Qué mide:**
- **La curva real de MB/s según las corrutinas.**
- **La velocidad real por conexión,** para compararla con los 3,30 MB/s que suponía el simulador.
- **La CPU y la memoria de la máquina.**
- **Los errores, 429/503 y repositorios abandonados.**
- **La cortesía,** verificada del lado del cliente.

**Duración:** unos 50 minutos de corrida. **Costo:** la t4g.large ~USD 0,11/h. S3 es casi gratis: unos 100–150 GB por un día y los PUT.

**Reglas de cortesía, ya en el código:**
- un pedido a la vez por repositorio, con 2 s desde el fin de uno hasta el siguiente;
- se respeta `Retry-After`;
- un repositorio se **abandona** ante un 403, un `Retry-After` de más de 5 minutos o tres errores de red seguidos;
- el User-Agent lleva tu correo (`CONTACTO`), para que un repositorio pueda avisar si algo le molesta;
- robots.txt no se consulta, por decisión tuya.

---

## 0. Código (en tu Mac)

```bash
cd ~/dev/lareferenciatests && git push origin subida-s3
```

La máquina clona la rama, y tiene que tener la muestra (commit `29e1b97`) y el modo muestra.

## 1. AWS, en **São Paulo (sa-east-1)**

Si ya los tenés de la prueba anterior, reusalos. Si no, se crean así (más detalle en `GUIA_PRODUCCION_SP.md`, paso 1):

1. **Bucket**, por ejemplo `prueba-concurrencia-mateo-sp`, con regla de ciclo de vida: borrar a 1 día y abortar los multipart incompletos.
2. **Rol IAM** `prueba-concurrencia-s3`, con su política sobre `arn:aws:s3:::<bucket>/prueba-concurrencia/*`.
3. **Key pair** `pruebareferencia-sp`, con el `.pem` en `prueba_concurrencia/claves/`.
4. **Instancia B:**

| Campo | Valor |
|---|---|
| Name | `candidata-sp` |
| AMI | Ubuntu Server 24.04 LTS, **64-bit (Arm)** |
| Instance type | **t4g.large** |
| Key pair | `pruebareferencia-sp` |
| Network | VPC por defecto, IP pública. Security group con **solo SSH (22) desde My IP**; la salida a internet viene abierta por defecto |
| Storage | 20 GiB gp3 |
| Advanced details → IAM instance profile | `prueba-concurrencia-s3` |
| Advanced details → Metadata response hop limit | **2** |
| Advanced details → Credit specification | **Unlimited** |

En tu Mac:

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/pruebareferencia-sp.pem
chmod 400 "$CLAVE"
IP_B=…        # la pública de la candidata
```

## 2. B: preparar

```bash
ssh -i "$CLAVE" ubuntu@"$IP_B" "sudo apt-get update && sudo apt-get install -y git tmux htop docker.io docker-compose-v2 && sudo usermod -aG docker ubuntu"
ssh -i "$CLAVE" ubuntu@"$IP_B" "git clone -b subida-s3 https://github.com/matespinetti/lareferenciatests.git"
ssh -i "$CLAVE" ubuntu@"$IP_B"
```

En B:

```bash
cd ~/lareferenciatests/prueba_concurrencia
nproc; free -m                                                       # 2 y unos 7.800 MB
wc -l ../relevamiento_exploratorio/muestra/items.csv                 # ~71.849: la muestra está
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml build motor
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml run --rm --no-deps motor \
  python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
```

El último comando tiene que mostrar un ARN con `prueba-concurrencia-s3`. Si dice `Unable to locate credentials`, falta poner el hop limit en 2.

**Ensayo corto (recomendado, ~3 minutos).** Un paso de 1 minuto, para ver que sale a internet, que sube a S3 y que no hay violaciones de cortesía:

```bash
export S3_BUCKET=prueba-concurrencia-mateo-sp AWS_REGION=sa-east-1 INSTANCIA=t4g.large CONTACTO=<tu correo>
DURACION=60 CALENTAMIENTO=10 RED_HOST=1 bash scripts/reales.sh r25
cat report.md | head -30
rm -rf results report.md      # que el informe final sea solo de la corrida completa
```

## 3. B: correr el barrido

```bash
tmux new -s reales
cd ~/lareferenciatests/prueba_concurrencia
export S3_BUCKET=prueba-concurrencia-mateo-sp AWS_REGION=sa-east-1 INSTANCIA=t4g.large CONTACTO=<tu correo>
RED_HOST=1 CARGA_WEB=1 bash scripts/reales.sh 2>&1 | tee run.log
```

Si se corta la conexión SSH, la corrida sigue en tmux; para volver, `tmux attach -t reales`.

Al terminar cada paso imprime `… N pedidos a M repositorios, 0 violaciones de cortesía, K abandonados, errores {…}`. Que haya errores y abandonos es normal: es justamente lo que el simulador no tenía.

En `report.md`, la sección **"Repositorios reales"** muestra:
- por paso: MB/s bajados y subidos, MB/s por corrutina, repositorios con trabajo, CPU de la máquina contra su línea base (30 %), memoria, retraso del bucle y cortesía;
- la **velocidad real por conexión** (p25, mediana y p75);
- los errores y los motivos de abandono.

## 4. Traer los resultados (en tu Mac)

```bash
mkdir -p ~/dev/lareferenciatests/prueba_concurrencia/resultados/sp-reales && cd ~/dev/lareferenciatests/prueba_concurrencia/resultados/sp-reales
R=lareferenciatests/prueba_concurrencia
scp -i "$CLAVE" -r ubuntu@"$IP_B":"$R/results" .
scp -i "$CLAVE" ubuntu@"$IP_B":"$R/report.md" ubuntu@"$IP_B":"$R/run.log" .
ls results/r25*
```

`results/r*-pedidos.csv` trae un pedido por fila. Con eso, Claude compara el barrido real con el del simulador (`resultados/sp/`) y actualiza el informe.

## 5. Limpiar

1. **EC2:** terminar `candidata-sp`.
2. **S3:** vaciar y borrar el bucket. La regla de 1 día ya lo va vaciando sola.
3. **Security group y key pair:** se pueden borrar. El rol IAM no cuesta nada si queda.
