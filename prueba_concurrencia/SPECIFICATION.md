# Prueba de concurrencia del motor

Nov 4, 2026 · @Mateo

## Objetivo

Decidir si el descargador necesita una arquitectura distribuida o si alcanza con **un solo proceso motor en una máquina chica de EC2**. La candidata es una instancia optimizada para red, la **c6gn.medium** (1 vCPU, 2 GB, 1,6 Gbit/s de red base, unos USD 0,043/h): con la red de una t3.small (0,128 Gbit/s) la descarga tardaría unos 85 días en vez de unos 34. El motor previsto usa corrutinas con E/S asíncrona (asyncio y aiohttp). Como referencia, se compara con la misma prueba hecha con hilos.

El problema real tiene 2.400 repositorios y 6.000.000 de ítems, repartidos de forma muy despareja. La prueba reproduce el pico de ese problema (todos los repositorios activos a la vez) y mide todo lo que podría agotarse: CPU, memoria, red, descriptores, el bucle de eventos y la base. Con esos números se proyecta el problema completo.

La prueba no toca repositorios reales: todo corre contra un servidor simulado.

## Dimensionamiento previo

Son cuentas a confirmar con la prueba, no resultados.

- **La cortesía pone un techo por repositorio.** Un repositorio recibe como mucho un pedido cada `t_pedido + 1 s`. Con la latencia de los pilotos (p50 de unos 0,5 s), eso da unos 0,6 pedidos/s por repositorio. Con 2.400 repositorios, el techo es del orden de 1.500 pedidos/s, y la concurrencia nunca supera 2.400 tareas.
- **Ese techo baja con el tiempo.** Los repositorios chicos terminan rápido y en las últimas semanas quedan activos solo los grandes.
- **El repositorio más grande fija la duración mínima.** Con unos 3 pedidos por ítem (resolución más 1 a 3 descargas), un repositorio de 300.000 ítems lleva unas dos semanas, haya una máquina o cien.
- **Lugares de ejecución necesarios:** `repos activos × t_pedido / (t_pedido + 1 s)`. Con 2.400 repositorios, del orden de 1.000 lugares.

Si un proceso sostiene ese pico con margen, distribuir no acelera nada: la duración la ponen la cortesía y la red, no el cómputo.

## Qué hay que construir

Dos programas en Python 3.12 con aiohttp, la misma biblioteca HTTP del proyecto.

Toda la E/S es asíncrona: HTTP con aiohttp, tanto en el cliente como en el servidor; y la base con asyncpg. Los archivos no se guardan: se hashean y se descartan, como en el descargador real. La única excepción es el modo hilos, que existe solo para comparar.

### Servidor simulado

Simula muchos repositorios, cada uno identificado por un prefijo de ruta (`/repo/<id>/...`). Está calibrado con lo medido en los pilotos del relevamiento exploratorio (`relevamiento_exploratorio/piloto*/pedidos.jsonl`).

| Medida | p50 | p95 | Otros |
| --- | --- | --- | --- |
| Latencia hasta los encabezados | 470 ms | 1,9 s | p99 de 4,2 s |
| Página de ítem | 10 KB | 450 KB | |
| Archivo | 0,5 MB | 38 MB | media de 11,5 MB, máximo de 200 MB, solo 51 archivos |

Comportamiento:

- **Latencia:** distribución log-normal configurable, resuelta con `asyncio.sleep`.
- **Respuestas:** página de ítem y archivo. El archivo se envía en streaming con un ancho de banda por conexión de 2 MB/s (`ANCHO_MB_S`). Es un supuesto que todavía no se midió y es el que más mueve la duración: si los repositorios entregan 0,5 MB/s, el repositorio más grande tarda unos 90 días en vez de unos 32, porque cada repositorio recibe un pedido a la vez. El informe trae una tabla de sensibilidad (0,5, 1, 2 y 5 MB/s), y `relevamiento medir-velocidad` lo mide sobre archivos de los pilotos. Así, un archivo grande tarda segundos, como en la realidad.
- **Tamaños:** por defecto siguen la distribución de los pilotos. También se puede fijar un tamaño.
- **Tope de red total:** en los escenarios del problema real, todo el servidor entrega como mucho lo que da la red base de la **instancia simulada** (unos 190 MB/s en una c6gn.medium, 15 MB/s en una t3.small). La instancia se elige con `INSTANCIA` entre los perfiles de `config.INSTANCIAS` (vCPU, memoria, red base y ráfaga, créditos de CPU y precio). Por loopback no hay placa de red: sin ese tope, 2.400 repositorios a 2 MB/s piden unos 3 GB/s, algo que la instancia real nunca recibiría, y el escenario mediría un proceso desbordado. El escenario 6 va sin tope, para medir cuánto aguanta un proceso.
- **Errores:** un porcentaje configurable de 429 con `Retry-After` y de 503.
- **HTTPS con un certificado autofirmado.** Los repositorios reales usan TLS, y el handshake cuesta CPU.
- **Conexiones:** un porcentaje configurable de repositorios cierra la conexión después de cada respuesta. Eso obliga a nuevos handshakes, como pasa con servidores reales.
- **Cabecera con el tiempo de servicio:** cada respuesta informa cuánto tardó el servidor. Así el motor puede calcular cuánta latencia agrega él mismo.
- **Control de cortesía desde afuera:** para cada repositorio registra los pedidos simultáneos y el tiempo entre el fin de un pedido y el inicio del siguiente. Cuenta como violación dos pedidos a la vez o un intervalo menor al configurado.

### Mini motor

Reproduce el núcleo del motor real.

- **Planificador en memoria** con un turno por repositorio: un pedido a la vez y un intervalo de 1 s contado desde el fin del pedido. Alcanza con una versión simplificada propia que aplique esa regla: no hace falta usar el planificador real del proyecto, porque lo que se mide es el costo de las tareas concurrentes, no el del planificador. Entre los repositorios habilitados atiende primero a los que tienen descargas pendientes: termina lo empezado antes de empezar otro ítem. Sin esto, con pocos lugares el arranque se iría en la primera resolución de los 2.400 repositorios.
- **Un nombre de host por repositorio** (`r<id>.sim.localhost`), resuelto a 127.0.0.1 por un resolver del motor. El servidor usa un certificado comodín `*.sim.localhost`. Así el cliente mantiene una conexión por repositorio, como con hosts reales, y no reutiliza una sola conexión para todos.
- **N lugares de ejecución** que toman tareas del planificador, hacen el pedido con un cliente HTTP compartido y devuelven el resultado.
- **Dos tipos de tarea:**
  - resolución: pide la página;
  - descarga: lee el archivo en streaming y calcula su SHA-256 como el fetcher real, descartando los bytes.
- **Cola inicial:** ítems repartidos entre los repositorios con la misma regla que usa el simulador del descargador (`LAR_descargador/simulator/src/lar_simulator/generate.py:_sizes`). El 4 % de los repositorios tiene un peso 48 veces mayor y concentra unos dos tercios de los ítems, como en la red. Con 2.545 repositorios y 6,1 M de ítems, eso da un máximo de unos 385.000 ítems y una mediana de unos 500. Las tareas se generan a medida que hacen falta, no como una cola de millones de objetos en memoria. Cada resolución genera entre 1 y 3 descargas.
- **`--modo hilos`:** la misma prueba con `ThreadPoolExecutor` y requests, para comparar memoria y CPU con las corrutinas. Es la referencia que muestra por qué se eligen corrutinas, o cuánto cuesta no hacerlo.
- **`--db`:** registrar cada resultado en un PostgreSQL local con una transacción, usando asyncpg y un pool de conexiones, para mostrar que la base tampoco es el cuello de botella.
## Qué medir

Una muestra por segundo, con psutil y contadores propios del motor. Las medidas del servidor solo sirven de control.

**Recursos del proceso motor**

- CPU separada en usuario y sistema, expresada en porcentaje de un núcleo.
- Memoria residente (RSS).
- Cantidad de hilos.
- Descriptores de archivo abiertos.
- Conexiones TCP abiertas.
- Cambios de contexto voluntarios e involuntarios.

**Recursos de la máquina**

- CPU total y tiempo robado (`steal`), que muestra si una instancia t3 se quedó sin créditos.
- Memoria disponible.
- Bytes por segundo en la red.

**Rendimiento**

- Tareas en curso y ocupación: el % de lugares de ejecución con una tarea.
- Pedidos por segundo, por tipo de tarea.
- Descargas completadas por segundo e ítems completos por segundo (resueltos y con todos sus archivos bajados).
- MB/s recibidos y MB/s hasheados.
- Latencia p50, p95 y p99 vista por el motor.

**Salud del motor**

Estos indicadores muestran si el motor, y no la cortesía, es lo que limita.

- **Retraso del bucle de eventos:** una corrutina duerme 100 ms y mide cuánto se pasa. Se reporta p50, p99 y máximo. En modo hilos, la medida equivalente es la demora de un hilo que duerme.
- **Retraso de despacho:** desde que una tarea puede salir por cortesía hasta que un lugar la toma.
- **Repositorios listos sin atender:** repositorios que podrían recibir un pedido y esperan un lugar libre.
- **Latencia agregada por el motor:** la latencia que ve el motor menos el tiempo de servicio que informa el servidor.
- **Pausas del recolector de basura:** cantidad y duración, medidas con `gc.callbacks`.

**Medidas derivadas**

- CPU por pedido y CPU por MB descargado.
- RSS por lugar de ejecución, como la pendiente entre escenarios.

**Errores, base y cortesía**

- Errores del cliente: timeouts, conexiones reiniciadas y "too many open files". Fuera de los 429 y 503 simulados, deberían ser cero.
- Con `--db`: transacciones por segundo, latencia de commit p95 y espera por una conexión del pool.
- Violaciones de cortesía que informa el servidor. Deberían ser cero.
- Como control, la CPU del servidor simulado: si satura, el escenario no vale.

## Escenarios

Cada escenario dura 3 minutos después de 30 segundos de calentamiento, salvo el de resistencia. En los escenarios 1, 2, 3 y 5 se simulan al menos 10 repositorios por lugar de ejecución, para que la cortesía no limite la concurrencia. Los escenarios 4 y 6 a 9 reproducen el problema real: 2.400 repositorios con la distribución despareja.

| Escenario | Lugares de ejecución | Para qué |
| --- | --- | --- |
| 1 | 50 | Línea base |
| 2 | 200 | La configuración prevista |
| 3 | 1.000 | Margen |
| 4 | 2.400, problema real, con la red de la instancia simulada | El pico real: todos los repositorios activos a la vez |
| 5 | 5.000 | Buscar el límite |
| 6 | 2.400, problema real sin tope de red y con archivos de 38 MB (p95 de los pilotos) | Capacidad de un proceso: cuántos MB/s procesa antes de saturar un núcleo |
| 7 | 2.400, problema real con `--modo hilos` | Comparación con hilos en el pico real |
| 8 | 2.400, problema real con `--db` | Costo de registrar en la base |
| 9 | 2.400, problema real durante 1 hora | Resistencia: que la memoria se mantenga estable |

Los escenarios 1 a 3 también se corren con `--modo hilos`, para comparar las dos alternativas según la cantidad de lugares.

**Subida a S3 (`s250` y `ss250`).** El descargador va a subir los archivos a S3 con un proceso aparte. Estos dos escenarios repiten el problema real con 250 lugares (la rodilla medida), con y sin tope de red, agregando el esquema elegido:

- el motor guarda cada archivo mientras lo baja: los de menos de 8 MB en un spool en RAM (`/dev/shm`), los demás en un spool en disco;
- al terminar el archivo, lo pasa a `listos/` con un rename atómico, nombrado por repositorio, ítem y archivo;
- un proceso subidor aparte (boto3 con su gestor de transferencias, en su propio núcleo) lo sube a un S3 falso local, con un PUT o multipart en partes de 8 MB, con clave por repositorio (`repo/item/archivo`), y lo borra del spool; volver a subirlo sobrescribe el mismo objeto;
- si lo pendiente pasa 256 MB en RAM o 4 GB en disco, el motor deja de repartir tareas nuevas, pero lo que está en curso termina: la descarga nunca espera a la subida, porque eso dejaría abierta la conexión con el repositorio.

Se mide si el motor llegó a frenar, cuánto quedó pendiente, la CPU que le agrega al motor escribir el spool, la CPU del subidor y lo que se escribe a disco.

Para comparar dónde conviene que corra la subida, con el mismo spool: `s250` la hace un proceso aparte, `sh250` un pool de hilos (s3transfer) dentro del proceso del motor y `sc250` un pool de corrutinas (aiobotocore) dentro del bucle del motor, como propone hoy la especificación técnica. Se cambia con `MODO_SUBIDA` y la cantidad de subidas a la vez con `SUBIDA_PARALELO` (16).

**Un proceso contra un planificador y N workers (`m250` y `ms500`).** Un proceso planificador aplica la cortesía y reparte cada tarea por TCP (en la prueba, por 127.0.0.1) a N procesos workers con sus propias corrutinas (`WORKERS`, 2), que devuelven el resultado. Los mismos workers podrían correr en otras máquinas. `m250` lo compara con un solo proceso (`b250`) con la red de la instancia; `ms500`, sin tope de red y con 500 lugares (contra `bs500`), muestra si repartir pasa el techo de un núcleo. Se mide la CPU total (planificador más workers), la memoria total, el retraso de los bucles y el despacho.

**Barrido de lugares.** El problema real (2.400 repositorios) con 2, 5, 10, 25, 50, 100, 250, 500, 1.000 y 2.400 lugares, 30 s cada uno después de 10 s de calentamiento. Responde cuánto mejora el rendimiento al sumar tareas concurrentes y desde dónde deja de mejorar:

- `b<N>`: con la red de la instancia simulada. Con 2 MB/s por conexión, la red de una t3.small se llena con unos 10 lugares y la de una c6gn.medium con unos 120.
- `bs<N>`: sin tope de red. El límite pasa a ser la CPU o la cortesía.

La **rodilla** es la menor cantidad de lugares que llega al 95 % de los MB/s máximos del barrido. Se usan MB/s y no ítems/s: en una corrida corta, con más lugares se terminan primero los ítems chicos. Con pocos lugares, la ocupación al 100 % y el despacho largo son lo esperado: hay más trabajo que lugares.

Para 1.000 lugares o más hay que subir el límite de archivos abiertos (`ulimit -n 65535`).

## Cómo correrlo en EC2

- **Una sola instancia: c6gn.xlarge** (4 vCPU Graviton2, 8 GB, unos USD 0,17/h) con Ubuntu 24.04. Es del mismo procesador que la candidata (c6gn.medium), así que la CPU por MB medida vale para ella.
- **Reparto de núcleos:** en Linux, `run_all.sh` ata el motor al núcleo 0 con `taskset` (emula una instancia de 1 vCPU y su CPU no se mezcla con la del servidor) y el servidor simulado a los demás, en hasta 4 procesos. Cada repositorio va siempre al mismo proceso del servidor (`puerto + repo % procesos`), así el control de cortesía sigue viendo todos sus pedidos.
- **La red se simula:** el tráfico va por loopback, así que la red de la máquina donde se corre no cuenta. El tope de red del servidor reproduce la red base de la instancia simulada; en una corrida corta, la ráfaga de una instancia real engañaría.
- **Otra candidata:** `INSTANCIA=c7g.large ./run_all.sh` repite la prueba con la red de otra instancia. El informe trae además una tabla con todas las candidatas: duración, corrutinas, CPU, memoria y costo total.
- **Créditos de CPU:** solo si se corre en una t3. En una corrida de semanas cuenta la línea base, no el pico; anotar el `CPUCreditBalance` de CloudWatch al principio y al final.
- No hace falta abrir puertos: todo va por localhost.
- Un script `run_all.sh` corre los escenarios y deja los resultados en `results/` y el informe en `report.md`. Tiene dos versiones:
  - **chica** (por defecto, la recomendada): escenarios 1, 2, 4, 6, 2h, 7, bs500, m250, ms500, s250, sh250, sc250 y ss250, de 1 minuto cada uno más 10 s de calentamiento, y el barrido con la red de la instancia simulada; unos 28 minutos en total. Conviene correrla en una c6gn.2xlarge (8 vCPU), para que el planificador, los workers, el subidor y el servidor simulado tengan núcleos propios;
  - **completa** (`./run_all.sh completa`): todos los escenarios con la duración de esta spec y los dos barridos, alrededor de 2 horas.
- Terminar la instancia al finalizar.

## Qué entregar

- `results/<escenario>.csv` con la serie por segundo.
- **`report.md`** con:
  - una tabla por escenario: lugares, modo, pedidos por segundo, MB/s, CPU media y máxima, RSS máxima, retraso del bucle p99, retraso de despacho p99, latencia p50 y p95, errores y violaciones de cortesía;
  - gráficos de CPU, memoria y retraso del bucle según la cantidad de lugares, con corrutinas y con hilos.
- **Proyección al problema real.** Con lo medido (CPU por pedido, RSS por lugar, MB/s por descarga) y el dimensionamiento previo, calcular:
  - CPU, memoria y red necesarias en el pico de 2.400 repositorios;
  - la duración total estimada, como la mayor de dos cotas: la del repositorio más grande, por la cortesía, y la de los bytes totales divididos por el ancho de banda de la instancia;
  - los MB/s máximos que procesa un proceso (TLS, HTTP y hash), comparados con la red de la instancia: si el proceso procesa más de lo que da la placa de red, la CPU no es el límite;
  - la comparación con la instancia simulada (CPU que sostiene, memoria y red) y una tabla con todas las candidatas: duración, corrutinas necesarias, CPU, memoria y costo on-demand de toda la descarga.
- **Barrido:** tabla y gráfico de MB/s, memoria y CPU según los lugares, la rodilla y el costo de cada lugar de más (memoria y CPU).
- **A lo largo de toda la descarga:** un modelo de fluido con la distribución de ítems. Mientras hay repositorios activos, todos avanzan al ritmo de la cortesía o, si la red no alcanza, a la parte de la red que les toca; los chicos terminan primero. Para la red base, la ráfaga y sin tope da:
  - la duración total;
  - los repositorios activos y los lugares necesarios en los días 0, 1, 7, 14, 30 y 60.

  Lugares necesarios: los que pide la cortesía o los que llenan la red, lo que sea menor.
- **Conclusión:** cuántas corrutinas usar, si alcanza un proceso con corrutinas, en qué instancia, qué recurso se agota primero y cómo se comparan las corrutinas con los hilos. Si lo que se agota es la red de la instancia, se resuelve con una instancia con más red, no distribuyendo.
- Los datos de la instancia: tipo, vCPU, memoria, versión de Python y de aiohttp.

## Criterio de decisión

Propuesto, a acordar. Un proceso alcanza, y distribuir es innecesario, si en los escenarios 4 y 9 se cumple todo esto:

- Cero violaciones de cortesía y cero errores del cliente.
- Retraso de despacho p99 por debajo de 100 ms: el motor no es lo que limita.
- Retraso del bucle p99 por debajo de 50 ms.
- CPU media dentro de la línea base de la instancia elegida.
- RSS máxima por debajo de la mitad de la memoria.
- Memoria estable durante la hora del escenario 9.

## Qué esperamos ver

Son hipótesis a confirmar, no resultados:

- **Memoria:** la del motor crece poco con la cantidad de corrutinas. Lo que pesa son los buffers de las conexiones, no las tareas. Con hilos crece más, pero 2.400 hilos siguen entrando en 2 GB.
- **CPU:** en el pico real, la CPU del motor queda lejos de un núcleo completo. El costo está en TLS, en procesar HTTP y en calcular hashes, no en el planificador.
- **Bucle de eventos:** el retraso se mantiene en pocos milisegundos. Si sube, marca el límite real del proceso.
- **Cortesía:** cero violaciones en todos los escenarios.
- **Límite real:** lo que limita es la cortesía, que fija la duración, y el ancho de banda, que fija la instancia. No la CPU.
- **Rodilla:** con la red base de la t3.small, unas 10 corrutinas ya llenan la red; con la de una c6gn.medium, unas 120; sin tope, hacen falta cientos al principio, pero después del primer día quedan unos 100 repositorios activos y alcanzan menos de 100 lugares. Una corrutina de más cuesta memoria (su conexión TLS abierta) y casi nada de CPU.
