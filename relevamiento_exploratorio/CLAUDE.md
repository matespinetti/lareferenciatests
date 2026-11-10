# Relevamiento exploratorio

Script de un solo uso que sondea una muestra chica de repositorios (DSpace, OJS, EPrints) para decidir el diseño de un descargador a gran escala. No es una aplicación: se valora la claridad y la fidelidad de los datos por encima de la extensibilidad.

La especificación completa es la fuente de verdad: @SPECIFICATION.md
Si algo de este archivo o del código la contradice, manda la spec. Señalá la discrepancia, no la resuelvas en silencio.

## Prioridad absoluta: no molestar a ningún servidor

Puede correr desde una red compartida. Ante la duda, se abandona el host.

- **Todo pedido HTTP pasa por el componente de cortesía** (turno por host: lock + instante en que terminó el último pedido). Ningún otro módulo crea clientes httpx ni hace pedidos por su cuenta. Esto incluye cada salto de una redirección y la descarga de `robots.txt`.
- **No cambies los valores de la tabla "Cortesía"** de la spec (intervalos, topes, timeouts, `Range`, límites de tamaño) salvo que te lo pidan explícitamente.
- **Sin reintentos.** Única excepción: un certificado TLS inválido se registra y se repite una vez sin verificar.
- **Redirecciones a mano** (`follow_redirects=False`), hasta 5; cada salto respeta el turno de su host destino y cuenta como pedido.
- **Archivos nunca completos:** `Range: bytes=0-2047`, leer en streaming y cerrar la conexión tras los primeros bytes. Nunca uses `.content`/`.read()` sobre un archivo. Páginas y XML: cortar a 2 MB.
  - **Única excepción, pedida por el usuario (nov 2026): `medir-velocidad`**, para medir a qué velocidad entregan un archivo los repositorios. Usa el tipo de pedido `medicion` del mismo componente de cortesía y una cortesía más estricta (`CORTESIA_MEDICION`: 30 s entre pedidos a un host, 2 hosts a la vez, 10 pedidos por repositorio). Lee hasta 8 MB o 30 s por archivo, sin `Range`, y guarda solo los primeros 2 KB. Solo pide archivos ya verificados en los pilotos o, con `--desde-tamanos`, archivos que figuran en las páginas METS que guardó `medir-tamanos` (hasta 5 por repositorio, de distintos tamaños, salteando lo que `robots.txt` no permite). No uses `medicion` para otra cosa.
- **`medir-tamanos`, pedido por el usuario (nov 2026):** mide el tamaño de los archivos desde los metadatos OAI (METS o xoai) de los repositorios de los pilotos, para calibrar el volumen de la prueba de concurrencia. No descarga archivos: pide páginas enteras de `ListRecords` (tipo `pagina`, tope de 2 MB; si una queda cortada, se descarta el último registro), una sin fechas y una por ventana de fechas. Cortesía propia (`CORTESIA_TAMANOS`: 10 s entre pedidos a un host, 2 hosts a la vez, 8 pedidos por repositorio). Pondera por la cantidad de ítems de cada repositorio (`completeListSize`).
- **Abandono de host** (429, 503, anti-bot, dos 403 seguidos, tres errores de red seguidos): se abandona por el resto de la corrida y se registra el motivo. No agregues lógica de "probar de nuevo más tarde".
- El tope de 40 pedidos por repositorio incluye el reconocimiento. Lo que no se llega a probar se marca como `no probado`, no se omite.

## Qué no hacer sin pedir permiso

- **No ejecutes `relevar` contra repositorios reales** sin que el usuario lo pida. Para desarrollar, usá tests y `capturas/`.
- No hardcodees un correo en el User-Agent: el contacto es configurable (`pruebamateo/0.1 (contacto: …)`).

## Comandos

```bash
uv run pytest                                   # toda la suite, en segundos (tiempo virtual con looptime)
uv run relevamiento relevar entradas/repositorios.ejemplo.csv --items entradas/items.ejemplo.csv --plan   # sin pedidos
uv run relevamiento relevar REPOS.csv --contacto CORREO [--solo r1,r2] [--salida salida]   # pedidos reales: pedir permiso
uv run relevamiento verificar-cortesia --salida salida
uv run relevamiento medir-velocidad --plan                                   # qué archivos pediría, sin pedidos
uv run relevamiento medir-velocidad --contacto CORREO [--salida medicion]    # pedidos reales: pedir permiso
uv run relevamiento medir-velocidad --desde-tamanos tamanos --contacto CORREO --salida medicion-sp   # repositorios grandes (5 archivos c/u)
uv run relevamiento verificar-cortesia --salida medicion --medicion
uv run relevamiento medir-tamanos --plan                                     # qué repositorios y páginas, sin pedidos
uv run relevamiento medir-tamanos --contacto CORREO [--salida tamanos]       # pedidos reales: pedir permiso
uv run relevamiento verificar-cortesia --salida tamanos --tamanos
uv run relevamiento resumir --salida salida --repositorios REPOS.csv
```

## Arquitectura

Código en `src/relevamiento/`:

- `cortesia.py` es el **único** módulo que crea clientes httpx. Concentra turnos, presupuesto, redirecciones, TLS, abandono y robots. `test_arquitectura.py` lo hace cumplir.
- `detectores.py`, `extractores.py` y `parseo.py` son **funciones puras** sobre una `Respuesta` ya obtenida: sin I/O, sin async y sin acceso a la cortesía. Así se prueban con capturas y se reutilizan en el descargador.
- `relevar.py` orquesta qué pedir, en qué orden y cuándo cortar. Si `pedir` lanza `SinPedido` (presupuesto agotado o host abandonado), eso se registra como `no probado`, nunca se omite.
- **`pedidos.jsonl` es la fuente de tiempos, códigos y cortesía**, con cada pedido, incluidos errores, saltos y robots. **`eventos.jsonl`**, un desvío acordado de la spec, guarda lo que decide el orquestador: reconocimiento, ítems, resultado por método, verificaciones, abandonos y `repo_terminado`, que es lo que permite reanudar. `resumir` deriva los CSV y `resumen.md` de ambos; no guardes datos que solo existan en los CSV.
- Por ítem se prueban **todos** los métodos aplicables, sin cortar en el primero que funcione. Cada URL candidata se verifica **una sola vez**. No se verifican los bundles THUMBNAIL, TEXT, LICENSE ni CC-LICENSE, ni un archivo con el mismo nombre y bundle que otro ya verificado bien.
- Desvío acordado: tope total de 120 s por pedido (`timeout_total` en `config.py`).

## Stack y convenciones

- Python 3.12 con uv, asyncio, httpx (HTTP/1.1), lxml, protego (robots.txt), typer y pytest.
- Nombres de comandos, archivos de salida, columnas y mensajes en español, tal como en la spec.

## Tests

- Los tests **nunca hacen red**. `tests/conftest.py` arma un internet falso con `httpx.MockTransport`. `tests/repos_falsos.py` simula cinco repositorios completos y `tests/muestras.py` tiene las respuestas sintéticas.
- El tiempo es virtual (`--looptime`): la cortesía usa el reloj del event loop, así que un test con esperas de minutos tarda milisegundos. No uses `time.sleep` ni `time.time` en el código.
- Las muestras son sintéticas. Después del piloto, sumá capturas reales y ajustá detectores y extractores contra ellas.
- Después de cualquier corrida real, `verificar-cortesia` sobre `pedidos.jsonl` tiene que pasar. Si falla, el bug está en el componente de cortesía, no en el verificador.
