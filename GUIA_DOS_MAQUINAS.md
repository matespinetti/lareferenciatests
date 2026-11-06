# Guía: validar en una c6gn.medium real (dos máquinas, Docker, PostgreSQL y S3 real)

Hasta ahora la c6gn.medium se **simuló**: el motor atado a un núcleo de una c6gn.xlarge, con la red limitada por el servidor simulado. Esta corrida la reemplaza por la máquina de verdad:

```
A: c6gn.large  ("los repositorios")               B: c6gn.medium  (la candidata)
   servidor simulado en 0.0.0.0:8443-8444   ◄────    Docker Compose:
   (2 procesos, tope de red = red base               postgres:16
    de la c6gn.medium)                               motor (+ subidor como proceso hijo)
   S3 falso en 0.0.0.0:9443                 ◄────     └──► S3 real en las corridas cortas,
                                                            S3 falso de A en la de 1 hora
```

Qué se mide que antes era estimado:
- la placa de red real;
- el EBS real del spool;
- los 2 GB reales con PostgreSQL al lado;
- Docker;
- la subida a S3 de verdad, en las corridas cortas.

La API y el frontend todavía no existen, así que siguen estimados.

Tiempo: alrededor de 2 horas, de las que 1 es la corrida larga. Costo: **unos USD 0,45**.
- Las dos instancias, unas 2 horas: unos USD 0,26 (c6gn.large USD 0,086/h y c6gn.medium USD 0,043/h), más USD 0,02 de las IP públicas.
- S3 real, solo en `s120` y `v120`: unos 40 GB y 20.000 pedidos, alrededor de USD 0,10. Se borra el mismo día, así que el almacenamiento queda en centavos. La transferencia de EC2 a S3 dentro de la misma región es gratis.
- **La corrida de 1 hora sube al S3 falso de A.** Con S3 real serían unos 600 GB y 240.000 PUT, más de USD 2. Lo que se mide en esa hora está en B: memoria estable, el EBS después de la ráfaga y la CPU de subir. B hace exactamente lo mismo con uno u otro S3: el mismo boto3 y los mismos bytes saliendo por su placa de red. Que S3 real responde bien ya lo muestran las corridas cortas.

Abreviaturas de la guía:
- **`$IP_A`** e **`$IP_B`**: las IP **públicas** de cada instancia, para entrar por SSH. Se definen en la Mac, en cada terminal nueva, junto con la clave:

  ```bash
  CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/pruebareferencia.pem
  IP_A=…        # pública del simulador
  IP_B=…        # pública de la candidata
  ```

- **`$IP_PRIVADA_A`**: la IP privada de A, la que usa B para pedirle. Se define **dentro de B** (paso 5), porque los comandos que la usan corren ahí.

---

## 0. Código

En tu Mac, desde `~/dev/lareferenciatests`, con todo commiteado:

```bash
git push
```

## 1. Bucket de S3 (consola de AWS, us-east-1)

S3 → **Create bucket**:

| Campo | Valor |
|---|---|
| Name | `prueba-concurrencia-<algo tuyo>`, por ejemplo `prueba-concurrencia-mateo-2026`. Tiene que ser único en todo AWS. |
| Region | **us-east-1**, la misma de las instancias |
| Block all public access | Activado, como viene por defecto |

Después, en el bucket → **Management → Create lifecycle rule**:
- Name `borrar`, aplicada a todo el bucket.
- Marcá **Expire current versions of objects** a **1 día** y **Delete expired object delete markers or incomplete multipart uploads** a **1 día**.

Es la red de seguridad por si te olvidás de vaciarlo en el paso 9.

## 2. Rol para que B pueda subir (IAM)

IAM → Roles → **Create role**:
1. Trusted entity: **AWS service → EC2**.
2. Sin políticas administradas: **Next** y nombralo `prueba-concurrencia-s3`.
3. Ya creado, en el rol → **Add permissions → Create inline policy → JSON**, cambiando `NOMBRE_DEL_BUCKET`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": "arn:aws:s3:::NOMBRE_DEL_BUCKET/prueba-concurrencia/*"
    }
  ]
}
```

Solo puede escribir bajo `prueba-concurrencia/` en ese bucket: ni leer, ni borrar, ni tocar otra cosa.

## 3. Las dos instancias

Primero A y después B, porque B va en la misma subred y el mismo security group que A.

**A: el servidor simulado.** Una **c6gn.large** (2 vCPU, 4 GB) alcanza.
- En la corrida de la medium simulada, el servidor sirviendo los 190 MB/s usó en total 56 % de un núcleo de media (72 % de pico), y el S3 falso 21 % (48 % de pico). En 2 núcleos queda cada uno en ~40 %.
- Su red base (~390 MB/s) cubre lo que manda (190) y lo que recibe del S3 falso (~170).
- Una c6gn.medium no alcanza: los picos pasan un núcleo, y su red base es justo los 190 MB/s que tiene que servir.
- Si igual llegara a ser el límite, el informe lo marca con ⚠ en "CPU servidor máx".

EC2 → **Launch instance**, como en `GUIA_EC2.md`:

| Campo | Valor |
|---|---|
| Name | `simulador` |
| AMI | **Ubuntu Server 24.04 LTS, 64-bit (Arm)** |
| Instance type | **c6gn.large** |
| Key pair | `pruebareferencia`: el par sigue existiendo aunque hayas terminado la instancia anterior |
| Network settings | VPC por defecto, *Auto-assign public IP*: **Enable**. Security group nuevo, `prueba-concurrencia`, con **SSH (22)** desde **My IP**. |
| Storage | 20 GiB gp3 |

**B: la candidata.** EC2 → **Launch instance**:

| Campo | Valor |
|---|---|
| Name | `candidata` |
| AMI | **Ubuntu Server 24.04 LTS, 64-bit (Arm)** |
| Instance type | **c6gn.medium** |
| Key pair | el mismo de A (`pruebareferencia`) |
| Network settings → Edit | La **misma VPC y la misma subred** que A: mirá la subred de A en su pestaña *Networking*. Misma subred quiere decir misma zona, y entre zonas el tráfico se cobra. *Select existing security group*: **el mismo de A**. |
| Storage | **20 GiB gp3**, con los 125 MB/s por defecto: como iría en producción |
| Advanced details → IAM instance profile | `prueba-concurrencia-s3` |
| Advanced details → Metadata response hop limit | **2**. Sin esto, el contenedor no llega a las credenciales del rol: Docker suma un salto. |

**Security group** (el que comparten las dos). Agregá una regla de entrada en *Edit inbound rules*:
- Custom TCP, puertos **8443-8444**, y otra igual con el puerto **9443** (el S3 falso).
- Source, en las dos: **el mismo security group**: escribí `sg-` y elegilo.

Así solo B (y A) llegan al servidor simulado; desde internet sigue cerrado.

Anotá:
- de A, la **Private IPv4** (`$IP_PRIVADA_A`) y la pública (`$IP_A`);
- de B, la pública (`$IP_B`).

## 4. A: levantar el servidor simulado

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/pruebareferencia.pem
ssh -i "$CLAVE" ubuntu@"$IP_A"
```

Adentro, la primera vez (el script instala `uv`, sube los límites de conexiones, arma los certificados y corre los tests):

```bash
sudo apt-get update && sudo apt-get install -y git tmux htop
git clone -b subida-s3 https://github.com/matespinetti/lareferenciatests.git
cd lareferenciatests/prueba_concurrencia && bash scripts/setup_ec2.sh
exit
```

Salir y volver a entrar hace falta para que tome el límite de archivos abiertos. Después:

```bash
tmux new -s servidor            # o: tmux attach -t servidor
cd ~/lareferenciatests/prueba_concurrencia
ulimit -n 65535
HOST=0.0.0.0 .venv/bin/s3falso --certs certs &
HOST=0.0.0.0 SERVIDOR_PROCESOS=2 ANCHO_MB_S=2.15 .venv/bin/servidor --escenario v120 --certs certs
```

El servidor y el S3 falso comparten los 2 núcleos: no se atan a ninguno. Tiene que decir:

```
S3 falso en https://s3.sim.localhost:9443 (0.0.0.0)
servidor simulado en https://*.sim.localhost:8443-8444 (0.0.0.0) (escenario v120, red de c6gn.medium: 190.7 MB/s, 2 proceso(s))
```

Dejalos corriendo toda la prueba: cada escenario de B reinicia sus contadores. Al terminar: `Ctrl+C` para el servidor y `pkill -f bin/s3falso` para el S3 falso.

- **`ANCHO_MB_S=2.15`:** la velocidad por conexión que mediste desde EC2, la misma de las corridas anteriores. En B va la misma.
- **El tope de 190,7 MB/s** es la red **base** de la c6gn.medium. Se deja a propósito: en una corrida corta, la medium tiene ráfaga de red y bajaría más de lo que sostiene en semanas. El tráfico igual pasa por las placas de red reales. Para ver la ráfaga, en una corrida aparte: `RED_MB_S=0`.

## 5. B: preparar la candidata

En otra terminal de tu Mac, primero copiá la CA de A a B (B la necesita para verificar el TLS del servidor simulado):

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/pruebareferencia.pem
scp -i "$CLAVE" ubuntu@"$IP_A":~/lareferenciatests/prueba_concurrencia/certs/ca.pem /tmp/ca-prueba.pem
ssh -i "$CLAVE" ubuntu@"$IP_B"
```

En B:

```bash
sudo apt-get update && sudo apt-get install -y git tmux htop docker.io docker-compose-v2
sudo usermod -aG docker ubuntu
exit
```

Salir y volver a entrar hace falta para que `ubuntu` quede en el grupo `docker`. Desde la Mac:

```bash
ssh -i "$CLAVE" ubuntu@"$IP_B" "git clone -b subida-s3 https://github.com/matespinetti/lareferenciatests.git && mkdir -p lareferenciatests/prueba_concurrencia/certs"
scp -i "$CLAVE" /tmp/ca-prueba.pem ubuntu@"$IP_B":~/lareferenciatests/prueba_concurrencia/certs/ca.pem
ssh -i "$CLAVE" ubuntu@"$IP_B"
```

Como el repositorio es privado, `git clone` pide usuario y un token de GitHub, igual que en `GUIA_EC2.md`.

En B, revisá tres cosas antes de correr:

```bash
echo "export IP_PRIVADA_A=…" >> ~/.bashrc && source ~/.bashrc   # la IP privada de A; queda para tmux y las sesiones nuevas
cd ~/lareferenciatests/prueba_concurrencia
mkdir -p results spool            # si no, Docker las crea como root en el chequeo de abajo
nproc; free -m                    # 1 y unos 1.900 MB
curl -s --cacert certs/ca.pem --resolve control.sim.localhost:8443:"$IP_PRIVADA_A" \
  https://control.sim.localhost:8443/__control/estado | head -c 80; echo
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml build motor
IP_SERVIDOR=x docker compose -f docker/compose.candidata.yaml run --rm --no-deps motor \
  python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
```

- El `curl` tiene que devolver un JSON que empieza con `{"pedidos"`. Si se queda colgado, es el security group.
- El último comando tiene que mostrar un ARN que termina en `prueba-concurrencia-s3/i-...`. Si dice `Unable to locate credentials`, falta el **hop limit en 2**. Se cambia sin relanzar: *Actions → Instance settings → Modify instance metadata options*.

## 6. B: correr

```bash
tmux new -s candidata
cd ~/lareferenciatests/prueba_concurrencia
export S3_BUCKET=NOMBRE_DEL_BUCKET ANCHO_MB_S=2.15
bash scripts/candidata.sh "$IP_PRIVADA_A" 2>&1 | tee run.log
```

Corre **b120, s120 y v120**, de 3,5 minutos cada uno: unos 12 minutos más el build.

| Escenario | Qué agrega |
|---|---|
| b120 | Solo descargar. Es la referencia. |
| s120 | Más spool y subida a S3, con el subidor en proceso aparte. |
| v120 | Más PostgreSQL: una transacción por resultado. Es el motor completo. |

En cada escenario tiene que decir algo como:

```
escenario v120 (corrutinas, red de c6gn.medium): 25000 pedidos, 0 violaciones, errores {}
```

`s120` y `v120` suben a S3 real; `b120` no sube.

Si todo da bien, la corrida larga. Dura una hora, sube al S3 falso de A (sin `S3_BUCKET`), agrega al mismo `results/` y rearma el informe con todo:

```bash
unset S3_BUCKET
bash scripts/candidata.sh "$IP_PRIVADA_A" v120h 2>&1 | tee -a run.log
```

Tiene que decir `S3_BUCKET vacío: la subida va al S3 falso en <IP privada de A>:9443.` En el informe, la columna "Subida" muestra cuál usó cada escenario.

**Para mirar mientras corre:** `Ctrl+b "` y `htop` en B; tiene un solo núcleo.
- En A, en otra ventana de tmux, `htop`: el servidor no tendría que llegar al 100 % en ningún núcleo.

**Qué esperar en la hora:** pasados unos 30 minutos se acaba la ráfaga de EBS de la medium. Si la escritura del spool en disco pasa lo que da el EBS de base, lo pendiente en disco crece y pueden bajar los MB/s. Si pasa, es un resultado, no un error: dice que en producción hace falta un volumen o una instancia con más EBS.

## 7. Revisar

```bash
less report.md
```

La sección que importa es **"Validación en una c6gn.medium real"**:

| Qué mirar | Bien |
|---|---|
| Violaciones / errores (descarga / subida) | 0 / 0 / 0 |
| MB/s bajados | cerca de 190 (el tope) en b120. Si s120 o v120 bajan mucho menos, algo de la medium limita: mirá CPU máquina y escritura a disco. |
| CPU máquina (media) | lejos de 100 % (es toda la instancia de 1 vCPU, PostgreSQL y Docker incluidos) |
| Memoria disponible mínima | holgada de los ~1.900 MB |
| Pico contenedor motor + pico PostgreSQL | ídem, y cuánto margen queda para la API y el frontend |
| Frenado (s) | 0: la descarga nunca esperó a la subida |
| Pendiente al final | chico y sin crecer |
| Bucle p99 | debajo de 50 ms |
| Deriva (v120h) | cerca de 0 |

## 8. Traer los resultados (en tu Mac)

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/pruebareferencia.pem
mkdir -p ~/dev/lareferenciatests/resultados-ec2-candidata && cd ~/dev/lareferenciatests/resultados-ec2-candidata
scp -i "$CLAVE" -r ubuntu@"$IP_B":~/lareferenciatests/prueba_concurrencia/results .
scp -i "$CLAVE" ubuntu@"$IP_B":~/lareferenciatests/prueba_concurrencia/report.md .
scp -i "$CLAVE" ubuntu@"$IP_B":~/lareferenciatests/prueba_concurrencia/run.log .
```

## 9. Limpiar (no te olvides)

1. **B:** EC2 → Instances → `candidata` → **Terminate instance**.
2. **A:** EC2 → Instances → `simulador` → **Terminate instance**.
3. **S3:** en el bucket, **Empty** (escribí `permanently delete`) y después **Delete**. Así no esperás un día a la regla de ciclo de vida, y el almacenamiento queda en centavos.
4. **IAM:** el rol `prueba-concurrencia-s3` se puede borrar; no cuesta nada si queda.
5. **Security group:** `prueba-concurrencia` se puede borrar cuando ya no quede ninguna instancia que lo use.

---

## Si algo falla

| Síntoma | Qué hacer |
|---|---|
| `!! El servidor simulado no responde en <IP privada de A>:8443` | ¿Está corriendo en A con `HOST=0.0.0.0`? ¿La regla del security group tiene como origen el mismo grupo? ¿Usaste la IP **privada**? |
| `!! Sin S3_BUCKET la subida va al S3 falso, y no responde` | En A, ¿está corriendo el `s3falso` con `HOST=0.0.0.0`? ¿La regla del puerto 9443? |
| `Unable to locate credentials` o `AccessDenied` en la subida (errores de subida > 0) | Hop limit en 2 (paso 5), el rol asignado a B y el nombre del bucket en la política y en `S3_BUCKET`. |
| `permission denied while trying to connect to the Docker daemon` | Faltó salir y volver a entrar después del `usermod` (paso 5). |
| El build se queda sin memoria | Raro en 2 GB. `docker system prune -f` y reintentá. |
| ⚠ en "CPU servidor máx" | El servidor simulado de A fue el límite: avisame. |
| Se cortó el SSH | `ssh …` y `tmux attach -t candidata` (en B) o `tmux attach -t servidor` (en A). |
