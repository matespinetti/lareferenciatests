# Prueba de concurrencia

Experimento descartable para responder una pregunta de arquitectura del descargador: **¿alcanza un solo proceso en una máquina chica (la candidata es una c6gn.medium: 1 vCPU, 2 GB, red optimizada) con corrutinas y E/S asíncrona, o hace falta una arquitectura distribuida?** Como referencia, se compara con hilos. El problema real tiene 2.400 repositorios y 6.000.000 de ítems con una distribución muy despareja. Se reproduce su pico, se mide todo lo que podría agotarse y se proyecta el problema completo. No es una aplicación: lo que importa es que los números sean confiables, no la extensibilidad del código.

La especificación completa es la fuente de verdad: @SPECIFICATION.md
Si algo de este archivo o del código la contradice, manda la spec. Señalá la discrepancia, no la resuelvas en silencio.

## Prioridad absoluta: que la medición sea válida

Un número equivocado lleva a una decisión de arquitectura equivocada. Ante la duda, se mide de menos y se aclara, nunca se infla.

- **El trabajo del motor tiene que ser real.** Leer el cuerpo completo en streaming, calcular el SHA-256 por chunk y parsear la respuesta HTTP de verdad. No acortes nada del lado del motor para "ir más rápido": ese es justamente el costo que se quiere medir. La latencia y el ancho de banda se simulan **solo en el servidor**, nunca en el motor.
- **La carga tiene que parecerse a la real.** Hay que respetar estas cuatro condiciones; si una no se cumple, el escenario subestima el costo:
  - HTTPS con handshake real;
  - un nombre de host por repositorio, para que haya una conexión por repositorio y no una sola compartida;
  - archivos que tardan segundos en transferirse;
  - latencias y tamaños calibrados con los pilotos (`relevamiento_exploratorio/piloto*/pedidos.jsonl`).
- **Nada puede limitar la concurrencia sin que se note.** `aiohttp.TCPConnector` trae `limit=100` por defecto: usá `limit=0` o uno mayor o igual a la cantidad de lugares. Lo mismo vale para el pool de conexiones de requests en modo hilos, el `backlog` del servidor y `ulimit -n`. Si los pedidos por segundo medidos quedan muy por debajo de lo esperable para los lugares y la latencia, hay un tope escondido: hay que encontrarlo antes de reportar. El retraso de despacho y los repositorios listos sin atender son los indicadores que lo delatan.
- **Sin placa de red, el loopback no pone límite.** Los escenarios del problema real usan un tope de red total en el servidor: la red base de la instancia simulada (`INSTANCIA`, perfiles en `config.INSTANCIAS`). Sin él miden un proceso desbordado por una demanda de GB/s que en EC2 nunca llegaría. El escenario 6 va sin tope a propósito: mide la capacidad de un proceso.
- **Una Mac no sirve para los números finales.** Con miles de conexiones TLS por loopback, macOS se queda sin memoria para sockets (error 55, `ENOBUFS`) y corta descargas. Los ensayos locales sirven para ver que todo anda; los números salen de EC2.
- **Los bytes se cuentan a medida que llegan**, no al terminar cada descarga. Un archivo grande a 2 MB/s tarda decenas de segundos: contarlo al final subestima los MB/s de las corridas cortas y corre la CPU por MB.
- **En el barrido, la ocupación al 100 % y el despacho de segundos con pocos lugares son lo esperado**, no un error: hay más trabajo que lugares. La rodilla se busca con MB/s, no con ítems/s.
- **Subida (s250, ss250): la descarga nunca espera a la subida.** El motor escribe el spool fuera del bucle (`asyncio.to_thread`) y entrega con un rename atómico; el subidor es otro proceso (no hilos del motor: competirían por el GIL). El único freno es dejar de repartir tareas nuevas, nunca dejar de leer una descarga en curso. En Linux, el subidor va atado al núcleo 1 y el servidor y el S3 falso a los demás.
- **El servidor simulado no puede ser el cuello de botella.** Las métricas del informe son solo del motor, pero registrá la CPU del servidor como control. Si satura un núcleo, el escenario no vale: hay que moverlo a otra instancia, como dice la spec.
- **Las métricas son del proceso del motor**, tomadas con psutil y contadores propios una vez por segundo. `cpu_percent` de un proceso puede pasar el 100 % con 2 vCPU: reportalo como porcentaje de un núcleo y aclaralo. El ancho de banda se cuenta en el motor: los contadores de la interfaz de loopback mezclan el tráfico del servidor.
- **Se corre en una sola máquina con los núcleos repartidos.** En Linux, el motor va atado al núcleo 0 y el servidor (varios procesos) a los demás; con menos de 3 núcleos compiten por la CPU y la medición no vale. Se corre en una c6gn.xlarge para simular una c6gn.medium: mismo procesador.
- **Una t3 engaña en corridas cortas.** Un escenario de 3 minutos gasta créditos de CPU que una corrida de semanas no tiene. Compará la CPU media con la línea base de la instancia, no con el 100 %, y mirá el `steal`.
- **Corrutinas e hilos se comparan en igualdad de condiciones:** el mismo planificador, la misma carga, la misma semilla y las mismas métricas.
- **El calentamiento no entra en las estadísticas.** Los 30 s iniciales quedan en el CSV, marcados, pero se excluyen de la tabla de `report.md`.
- **Las hipótesis y el dimensionamiento previo de la spec no son resultados.** `report.md` dice lo que se midió y separa con claridad lo medido de lo proyectado. Si un número contradice una hipótesis, se reporta tal cual, y la conclusión sobre la arquitectura sale de los datos y del criterio de decisión de la spec.

## E/S asíncrona

El motor previsto usa corrutinas. Si algo bloquea el bucle, la prueba mide un motor peor que el real.

- **HTTP con aiohttp**, en el cliente y en el servidor. **Base con asyncpg**. Los archivos se hashean y se descartan: no se escriben a disco.
- **El CSV de métricas se arma en memoria y se escribe al terminar la corrida**, así no hay E/S de disco sincrónica mientras se mide.
- **Prohibido en el código de corrutinas:**
  - `requests`, `time.sleep` y `open()`/`write()` sincrónicos;
  - `socket.getaddrinfo` directo;
  - cualquier llamada que espere red o disco sin `await`.
- **Esperas:** `asyncio.sleep`. **Tiempos:** `loop.time()`.
- **Modo hilos:** requests y `ThreadPoolExecutor` viven solo en su propio módulo. El código de corrutinas no lo importa.
- **El SHA-256 se calcula en el bucle, chunk por chunk**, como en el fetcher real. Es CPU y bloquea el bucle mientras procesa cada chunk. Si eso hace subir el retraso del bucle, es un resultado a reportar, no algo para esconder mandándolo a un executor. Usá chunks de tamaño fijo, por ejemplo 64 KB.
- **psutil se lee en el bucle una vez por segundo.** Son llamadas cortas al sistema, así que se toleran, pero que el muestreo sea lo único sincrónico.

## Cortesía

La cortesía no es lo que se mide, pero tiene que estar activa: el motor real la aplica, y sin ella la prueba no representa el caso real.

- Un turno por repositorio: un pedido a la vez y 1 s contado **desde el fin** del pedido (último byte leído). Basta con un planificador propio y simple. No hace falta el del proyecto.
- La clave de cortesía es el id del repositorio (prefijo `/repo/<id>/`), no la IP: todos resuelven a 127.0.0.1, y si la clave fuera la IP, todo se serializaría en un único turno.
- 429 y 503: se respeta `Retry-After` pausando ese repositorio. Sin reintentos inmediatos.
- **Cero violaciones** según el servidor en todos los escenarios. Si aparece una, el bug está en el motor, no en el contador del servidor. Ojo con los timeouts del cliente: si el motor corta un pedido mientras el servidor sigue atendiéndolo y libera el turno, el servidor lo cuenta como dos pedidos a la vez.
- En los escenarios sintéticos (1, 2, 3 y 5) se simulan al menos 10 repositorios por lugar de ejecución, para que la cortesía no sea lo que limita. Los del problema real (4 y 6 a 9) usan 2.400 repositorios con la distribución despareja.

## Qué no hacer sin pedir permiso

- **Nunca hagas pedidos a repositorios reales.** El servidor escucha solo en `127.0.0.1` y el motor solo le pide a él.
- **No crees, modifiques ni termines instancias de EC2** ni otros recursos de AWS sin que el usuario lo pida: cuestan plata. Al terminar una corrida en EC2, recordá terminar la instancia.
- No cambies los parámetros de los escenarios (lugares, duraciones, tamaños, latencias, intervalo de 1 s) salvo que te lo pidan.

## Comandos

```bash
uv run pytest                                   # toda la suite en segundos, solo localhost (test_db necesita CONCURRENCIA_DSN)
sh scripts/certificado.sh certs                 # CA y certificado *.sim.localhost (run_all.sh lo hace solo)
uv run servidor --escenario 4                   # servidor simulado en https://*.sim.localhost:8443
uv run motor --escenario 4 --salida results/4 [--duracion 15 --calentamiento 5] [--modo hilos]
uv run informe --resultados results --salida report.md
./run_all.sh                                    # versión chica (recomendada): 8 escenarios + barrido, ~19 min
./run_all.sh completa                           # todo con la duración de la spec y los dos barridos, ~2 h
DURACION=15 CALENTAMIENTO=5 ./run_all.sh 4 7     # algunos escenarios, acortados (ensayos)
bash scripts/setup_ec2.sh                       # una vez, en la instancia Ubuntu 24.04 (c6gn.xlarge)
INSTANCIA=c7g.large ./run_all.sh                # la misma prueba con la red de otra candidata
ANCHO_MB_S=0.8 ./run_all.sh                     # con la velocidad por conexión medida (relevamiento medir-velocidad)
```

Los escenarios (lugares, repositorios, modo, `--db`, duración) están en `config.ESCENARIOS`. El motor no recibe esos parámetros sueltos: así cada corrida es reproducible por su id.

## Arquitectura

Dos procesos separados, así las métricas del motor no incluyen al servidor:

- **Servidor simulado** (aiohttp, HTTPS). Atiende `/repo/<id>/...` con latencias y tamaños calibrados con los pilotos, archivos en streaming con ancho de banda por conexión, 429/503 configurables y una cabecera con su tiempo de servicio. Cuenta las violaciones de cortesía por repositorio y las expone al final de la corrida.
- **Mini motor.** Planificador en memoria (sin I/O, usa el reloj del event loop) + N lugares de ejecución que toman tareas, hacen el pedido con **un cliente HTTP compartido** y devuelven el resultado. Un resolver propio manda cada `r<id>.sim.localhost` a 127.0.0.1, y el certificado del servidor es un comodín `*.sim.localhost`. La resolución genera de 1 a 3 descargas. El muestreador de métricas y la medición del retraso del bucle corren en el mismo proceso, y el muestreador nunca bloquea el bucle.
- **Informe.** Lee los CSV, arma las tablas y los gráficos, y calcula la proyección al problema real (2.400 repositorios y 6.000.000 de ítems) con lo medido. Las cuentas de la proyección quedan explícitas en `report.md`, para que se puedan revisar.
- **Modo hilos.** Es la referencia para comparar, no la arquitectura prevista. Usa el mismo planificador y la misma carga, con `ThreadPoolExecutor` y requests. El planificador no es thread-safe: protegelo con un lock. Tiene que ser una comparación justa, con la misma lógica y sin atajos.
- **`--db`.** Una transacción por resultado contra PostgreSQL local con asyncpg y un pool de conexiones acotado.
- Todo azar (latencias, errores, reparto de ítems) usa una semilla configurable que queda registrada en los resultados.

## Stack y convenciones

- Python 3.12 con uv, asyncio, aiohttp, psutil, hashlib, ssl, asyncpg (`--db`), requests (solo en el modo hilos), matplotlib (gráficos) y pytest.
- Nombres de comandos, archivos, columnas y mensajes en español, como en la spec.
- Código simple y directo: es un experimento. Nada de capas ni abstracciones que la prueba no necesite.

## Tests

- Los tests solo usan localhost. Levantan servidor y motor con pocos lugares y duraciones de segundos.
- Lo mínimo que tiene que estar probado:
  - el servidor detecta violaciones cuando un cliente las provoca a propósito;
  - el motor no produce ninguna en una corrida corta;
  - el CSV tiene una fila por segundo con todas las columnas.
- Antes de lanzar los escenarios en EC2, corré uno corto en local y revisá que los pedidos por segundo coincidan con lo esperable para esa cantidad de lugares y esa latencia.

## Entregables

- `results/<escenario>.csv`, `report.md` con las tablas y los gráficos que pide la spec, y los datos de la instancia (tipo, vCPU, memoria, versión de Python y de aiohttp).
- `report.md` termina con una conclusión explícita sobre la pregunta de arquitectura, evaluada contra el criterio de decisión de la spec: si alcanza un proceso con corrutinas, cómo se compara con hilos, en qué instancia, qué recurso se agota primero (CPU, memoria, red, descriptores o retraso del bucle) y cuánto duraría el problema completo.
