# Guía: correr la prueba de concurrencia en EC2

Qué se hace, en orden:

1. Lanzar una **c6gn.2xlarge** (8 vCPU ARM, 16 GB). Con 8 núcleos, el planificador, los workers, el subidor y el servidor simulado tienen cada uno el suyo.
2. Medir desde ahí la velocidad real de los repositorios (`medir-velocidad`, unos 2 minutos).
3. Correr la prueba de concurrencia con esa velocidad (`run_all.sh`, unos 30 minutos): un proceso, multiproceso, la simulación de una c6gn.medium y los tres modos de subida a S3.
4. Traer los resultados a tu máquina.
5. **Terminar la instancia.**

Tiempo total: alrededor de 1 hora. Costo: unos USD 0,40 (USD 0,346/h la instancia, más USD 0,005/h la IP pública).

---

## 0. Subir el código (en tu Mac)

La carpeta `lareferenciatests` no es un repositorio git todavía. Hay dos caminos.

### Opción A: repositorio privado en GitHub

Creá un repositorio **privado** vacío en GitHub (sin README) y después:

```bash
cd ~/dev/lareferenciatests
git init
git add .
git status                     # revisá que no aparezcan .venv/, results/ ni certs/
git commit -m "Relevamiento y prueba de concurrencia"
git branch -M main
git remote add origin git@github.com:TU_USUARIO/lareferenciatests.git
git push -u origin main
```

El `.gitignore` deja afuera los entornos virtuales, `__pycache__` y los resultados locales de la prueba. Los pilotos (`relevamiento_exploratorio/piloto*`) sí se suben, porque `medir-velocidad` los necesita.

### Opción B: copiar la carpeta directo, sin git

Se hace después del paso 2, cuando la instancia ya existe:

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/prueba.pem
rsync -av --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude claves \
  -e "ssh -i $CLAVE" ~/dev/lareferenciatests ubuntu@IP_PUBLICA:~/
```

El `--exclude claves` es para que la clave privada no viaje a la instancia.

---

## 1. Lanzar la instancia (consola de AWS)

EC2 → **Launch instance**:

| Campo | Valor |
|---|---|
| Región (arriba a la derecha) | **us-east-1 (N. Virginia)**. Los precios del informe son de ahí. Si el descargador va a correr en otra región (por ejemplo São Paulo), usá esa: la medición de velocidad cambia con la distancia a los repositorios. |
| Name | `prueba-concurrencia` |
| AMI | **Ubuntu Server 24.04 LTS**, arquitectura **64-bit (Arm)**. Ojo: no la x86. |
| Instance type | **c6gn.2xlarge** |
| Key pair | Creá uno nuevo (RSA, `.pem`) llamado `prueba` y guardá el `prueba.pem` en **`prueba_concurrencia/claves/`**. Esa carpeta está excluida de git, igual que cualquier `.pem`. |
| Network settings | VPC por defecto, *Auto-assign public IP*: **Enable**. Security group nuevo con **SSH (22)** desde **My IP**, nada más. |
| Storage | **20 GiB gp3** |
| Purchasing option | On-demand, que es lo que viene por defecto. No uses spot: si AWS la corta, se pierde la corrida. |

**Launch instance.** Esperá a que esté *Running* y copiá la **Public IPv4 address**.

## 2. Entrar a la máquina

```bash
mv ~/Downloads/prueba.pem ~/dev/lareferenciatests/prueba_concurrencia/claves/   # si el navegador la bajó a Descargas
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/prueba.pem
chmod 400 "$CLAVE"
ssh -i "$CLAVE" ubuntu@IP_PUBLICA
```

`CLAVE` vale solo en esa terminal. Si abrís otra (por ejemplo, para el paso 8), definila de nuevo con la misma línea.

Ya adentro:

```bash
sudo apt-get update && sudo apt-get install -y git tmux htop
nproc            # tiene que decir 8
uname -m         # tiene que decir aarch64
```

**Usá tmux para todo lo que sigue.** Si se corta el SSH, la corrida sigue, y te reconectás con `tmux attach -t prueba`.

```bash
tmux new -s prueba
```

## 3. Bajar el código

**Opción A (git):**

```bash
git clone https://github.com/TU_USUARIO/lareferenciatests.git
```

Como el repositorio es privado, GitHub te va a pedir usuario y un **token**, no tu contraseña. Se crea en GitHub → Settings → Developer settings → Personal access tokens, con permiso de lectura del repositorio.

**Opción B (rsync):** corré el comando del paso 0 desde tu Mac.

## 4. Instalar todo

```bash
cd ~/lareferenciatests/prueba_concurrencia
bash scripts/setup_ec2.sh
source ~/.bashrc
```

El script hace todo esto:
- instala Postgres, compiladores y `uv`;
- sube los límites de conexiones;
- crea la base para el escenario 8;
- guarda los datos de la máquina en `results/instancia.json`;
- **corre los tests**.

Tiene que terminar con algo como `43 passed, 1 skipped` (o `44 passed`) y `Listo`. Si fallan los tests, no sigas: mirá la sección de problemas al final.

## 5. Medir la velocidad de los repositorios desde EC2

La velocidad medida desde tu casa no es la misma que desde AWS: cambia la distancia y la red. Vuelve a pedir los 28 archivos de los pilotos, con la cortesía estricta (30 s entre pedidos a un host, 2 hosts a la vez, hasta 8 MB o 30 s por archivo).

```bash
cd ~/lareferenciatests/relevamiento_exploratorio
uv run relevamiento medir-velocidad --plan          # lista lo que va a pedir; no hace pedidos
uv run relevamiento medir-velocidad --contacto pepesand@gmail.com --en-horario-ok --salida medicion-ec2
uv run relevamiento verificar-cortesia --salida medicion-ec2 --medicion
cat medicion-ec2/velocidad.json
```

- `--en-horario-ok` hace falta porque la máquina está en hora UTC, y el chequeo de horario laboral la toma así.
- `verificar-cortesia` tiene que terminar en **OK**.
- En `velocidad.json` buscá **`mb_s_por_repositorio` → `p50`**: es la velocidad típica por conexión. Anotá también el `p25`, que es el caso pesimista.

En tu casa dio p50 = 3,3 y p25 = 1,1.

## 6. Correr la prueba de concurrencia

Reemplazá `3.3` por el p50 que te dio el paso 5:

```bash
cd ~/lareferenciatests/prueba_concurrencia
ANCHO_MB_S=3.3 ./run_all.sh 2>&1 | tee run.log
```

Tarda **unos 30 minutos**. Para correr solo la simulación de la c6gn.medium (unos 3 minutos): `DURACION=60 CALENTAMIENTO=10 ANCHO_MB_S=2.15 ./run_all.sh b120 s120`. Al arrancar tiene que decir:

```
Instancia simulada: c6gn.medium · servidor en 3 proceso(s) · núcleos: 4
```

Y en cada escenario, algo como:

```
== escenario 4
servidor simulado en https://*.sim.localhost:8443-8445 (escenario 4, red de c6gn.medium: 190.7 MB/s, 3 proceso(s))
escenario 4 (corrutinas, red de c6gn.medium): 12345 pedidos, 0 violaciones, errores {}
```

Lo importante es que diga **0 violaciones** y que `errores {}` esté vacío.

El reparto de núcleos cambia según el escenario:
- un proceso: el motor en la CPU 0 y el servidor simulado en las demás;
- `m250` y `ms500` (multiproceso): el planificador en la CPU 0, los dos workers en la 1 y la 2, y el servidor en las demás;
- `s250`, `sh250`, `sc250` y `ss250` (subida a S3): el motor en la CPU 0, el subidor en la 1 (solo en `s250` y `ss250`, que lo tienen en un proceso aparte), y el servidor y el S3 falso en las demás.

**Para mirar mientras corre:** dividí tmux con `Ctrl+b` y después `"`, y corré `htop`. Tenés que ver la CPU 0 ocupada por el motor y las CPU 1 a 3 por el servidor.

### Corridas opcionales

Cada corrida escribe en `results/`, así que **mové los resultados antes de la siguiente** para que no se mezclen:

```bash
mv results results-c6gn-medium-p50 && mv report.md report-c6gn-medium-p50.md
```

| Para ver… | Comando |
|---|---|
| El caso pesimista (repositorios lentos) | `ANCHO_MB_S=1.1 ./run_all.sh` (con el p25 que te dio) |
| Otra candidata, de 2 vCPU | `INSTANCIA=c6gn.large ANCHO_MB_S=3.3 ./run_all.sh` |
| Todo, con la duración de la spec (~2 h) | `ANCHO_MB_S=3.3 ./run_all.sh completa` |

## 7. Revisar rápido antes de bajar

```bash
less report.md
```

Fijate en esto:
- **Escenarios:** violaciones en 0, errores en 0, y que ninguna fila tenga ⚠ en "CPU servidor máx".
- **Barrido:** dónde está la rodilla.
- **Subida a S3:** "Frenado (s)" en 0 (la descarga nunca esperó a la subida), lo pendiente chico, cuánto CPU le agrega al motor y cuánto usa el subidor, y la escritura a disco (el volumen gp3 da 125 MB/s de base).
- **Instancias candidatas:** la fila marcada con ←, la CPU (si tiene ⚠, 1 vCPU no alcanza) y el costo.
- **Criterio de decisión:** qué cumple y qué no.

## 8. Traer los resultados (en tu Mac, en otra terminal)

```bash
CLAVE=~/dev/lareferenciatests/prueba_concurrencia/claves/prueba.pem
mkdir -p ~/dev/lareferenciatests/prueba_concurrencia/resultados/ec2
cd ~/dev/lareferenciatests/prueba_concurrencia/resultados/ec2
scp -i "$CLAVE" -r ubuntu@IP_PUBLICA:~/lareferenciatests/prueba_concurrencia/results .
scp -i "$CLAVE" ubuntu@IP_PUBLICA:~/lareferenciatests/prueba_concurrencia/report.md .
scp -i "$CLAVE" ubuntu@IP_PUBLICA:~/lareferenciatests/prueba_concurrencia/run.log .
scp -i "$CLAVE" -r ubuntu@IP_PUBLICA:~/lareferenciatests/relevamiento_exploratorio/medicion-ec2 .
```

Si hiciste corridas opcionales, bajá también las carpetas `results-*` y los `report-*.md`.

## 9. Terminar la instancia (no te olvides)

EC2 → Instances → seleccionala → **Instance state → Terminate instance**.

Después revisá dos cosas:
- **Volumes:** que no quede ningún volumen suelto. Por defecto se borra con la instancia.
- **Elastic IPs:** que no haya ninguna. Si no creaste ninguna, está bien.

Una instancia olvidada prendida cuesta unos USD 125 por mes.

---

## Si algo falla

| Síntoma | Qué hacer |
|---|---|
| Fallan los tests en `setup_ec2.sh` al compilar algo | Volvé a correr `uv sync` y mirá el error. El script ya instala `build-essential`. Pasame el mensaje. |
| `!! Con menos de 3 núcleos…` | Elegiste otra instancia. Tiene que ser c6gn.2xlarge, con 8 vCPU. |
| `Address already in use` | Quedó un servidor de una corrida cortada: `pkill -f bin/servidor` y volvé a correr. |
| Se cortó el SSH | `ssh …` y después `tmux attach -t prueba`. La corrida siguió. |
| En el informe aparece ⚠ en "CPU servidor máx" | El servidor simulado (o el S3 falso, en `s250` y `ss250`) fue el límite en ese escenario. Avisame: se puede repetir en una c6gn.2xlarge (8 vCPU), que tiene más núcleos para el servidor. |
| `medir-velocidad`: "Es horario laboral" | Agregá `--en-horario-ok`. |
| `verificar-cortesia` no da OK | No sigas pidiendo a los repositorios. Pasame la salida. |
| `git clone` pide contraseña y falla | Usá un token de GitHub en vez de la contraseña, o la opción B (rsync). |

Con `results/`, `report.md` y `medicion-ec2/` en tu Mac, avisame y armamos la conclusión.
