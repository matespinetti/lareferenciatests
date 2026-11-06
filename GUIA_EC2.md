# Guía: correr la prueba de concurrencia en EC2

Qué se hace, en orden:

1. Lanzar una **c6gn.xlarge** (4 vCPU ARM, 8 GB).
2. Medir desde ahí la velocidad real de los repositorios (`medir-velocidad`, unos 2 minutos).
3. Correr la prueba de concurrencia con esa velocidad (`run_all.sh`, unos 16 minutos).
4. Traer los resultados a tu máquina.
5. **Terminar la instancia.**

Tiempo total: alrededor de 1 hora. Costo: unos USD 0,20 (USD 0,173/h la instancia, más USD 0,005/h la IP pública).

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
rsync -av --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  -e "ssh -i ~/Downloads/prueba.pem" ~/dev/lareferenciatests ubuntu@IP_PUBLICA:~/
```

---

## 1. Lanzar la instancia (consola de AWS)

EC2 → **Launch instance**:

| Campo | Valor |
|---|---|
| Región (arriba a la derecha) | **us-east-1 (N. Virginia)**. Los precios del informe son de ahí. Si el descargador va a correr en otra región (por ejemplo São Paulo), usá esa: la medición de velocidad cambia con la distancia a los repositorios. |
| Name | `prueba-concurrencia` |
| AMI | **Ubuntu Server 24.04 LTS**, arquitectura **64-bit (Arm)**. Ojo: no la x86. |
| Instance type | **c6gn.xlarge** |
| Key pair | Creá uno nuevo (RSA, `.pem`) y bajalo, por ejemplo `prueba.pem`. |
| Network settings | VPC por defecto, *Auto-assign public IP*: **Enable**. Security group nuevo con **SSH (22)** desde **My IP**, nada más. |
| Storage | **20 GiB gp3** |
| Purchasing option | On-demand, que es lo que viene por defecto. No uses spot: si AWS la corta, se pierde la corrida. |

**Launch instance.** Esperá a que esté *Running* y copiá la **Public IPv4 address**.

## 2. Entrar a la máquina

```bash
chmod 400 ~/Downloads/prueba.pem
ssh -i ~/Downloads/prueba.pem ubuntu@IP_PUBLICA
```

Ya adentro:

```bash
sudo apt-get update && sudo apt-get install -y git tmux htop
nproc            # tiene que decir 4
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

Tarda **unos 16 minutos**. Al arrancar tiene que decir:

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
- **Instancias candidatas:** la fila marcada con ←, la CPU (si tiene ⚠, 1 vCPU no alcanza) y el costo.
- **Criterio de decisión:** qué cumple y qué no.

## 8. Traer los resultados (en tu Mac, en otra terminal)

```bash
mkdir -p ~/dev/lareferenciatests/resultados-ec2
cd ~/dev/lareferenciatests/resultados-ec2
scp -i ~/Downloads/prueba.pem -r ubuntu@IP_PUBLICA:~/lareferenciatests/prueba_concurrencia/results .
scp -i ~/Downloads/prueba.pem ubuntu@IP_PUBLICA:~/lareferenciatests/prueba_concurrencia/report.md .
scp -i ~/Downloads/prueba.pem ubuntu@IP_PUBLICA:~/lareferenciatests/prueba_concurrencia/run.log .
scp -i ~/Downloads/prueba.pem -r ubuntu@IP_PUBLICA:~/lareferenciatests/relevamiento_exploratorio/medicion-ec2 .
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
| `!! Con menos de 3 núcleos…` | Elegiste otra instancia. Tiene que ser c6gn.xlarge, con 4 vCPU. |
| `Address already in use` | Quedó un servidor de una corrida cortada: `pkill -f bin/servidor` y volvé a correr. |
| Se cortó el SSH | `ssh …` y después `tmux attach -t prueba`. La corrida siguió. |
| En el informe aparece ⚠ en "CPU servidor máx" | El servidor simulado fue el límite en ese escenario. Avisame: se puede repetir en una c6gn.2xlarge (8 vCPU), que tiene más núcleos para el servidor. |
| `medir-velocidad`: "Es horario laboral" | Agregá `--en-horario-ok`. |
| `verificar-cortesia` no da OK | No sigas pidiendo a los repositorios. Pasame la salida. |
| `git clone` pide contraseña y falla | Usá un token de GitHub en vez de la contraseña, o la opción B (rsync). |

Con `results/`, `report.md` y `medicion-ec2/` en tu Mac, avisame y armamos la conclusión.
