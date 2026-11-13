# Relevamiento exploratorio de repositorios

Oct 28, 2026 · @Mateo

Script independiente que recorre una muestra chica de repositorios institucionales (DSpace, OJS y EPrints) para averiguar cómo se llega desde la URL de un ítem a sus archivos. No es una aplicación: es una prueba para tomar decisiones de diseño de un descargador a gran escala. La prioridad absoluta es no molestar a ningún servidor, porque puede correr desde una red compartida.

## Qué tiene que averiguar

- Si la plataforma y la versión de cada repositorio se pueden detectar, y cómo.
- Qué métodos de resolución funcionan según plataforma y versión, y cuántos pedidos necesita cada uno por ítem.
- Cuántos archivos tiene cada ítem, de qué tipo, y si en DSpace alcanza con el bundle ORIGINAL.
- Qué proporción de URLs son handles, DOIs, páginas o archivos directos.
- Qué tan seguido aparecen desafíos anti-bot (Cloudflare, Anubis), respuestas 403 o 429 y certificados TLS inválidos.
- Qué latencias tienen los servidores.

## Cortesía

Las reglas son deliberadamente más estrictas que las de cualquier crawler razonable: ante la duda, el script abandona el servidor.

| Regla | Valor |
| --- | --- |
| Identificación | User-Agent `pruebamateo/0.1 (contacto: <tu correo>)` |
| Pedidos simultáneos | Nunca más de uno por host |
| Intervalo por host | 5 s desde que termina un pedido hasta que empieza el siguiente, o el doble de lo que tardó la última respuesta si es mayor |
| Hosts en paralelo | 3 como máximo |
| Pedidos por repositorio | 40 como máximo, contando el reconocimiento |
| Archivos | Nunca se descargan completos: se piden con `Range: bytes=0-2047` y se cierra la conexión tras leer los primeros bytes |
| Páginas y XML | Hasta 2 MB por respuesta |
| Tiempos de espera | 10 s para conectar y 30 s sin recibir datos |
| Redirecciones | Hasta 5, seguidas una por una; cada salto respeta las reglas de su host de destino |
| Reintentos | Ninguno |
| Abandono de un host | Ante un 429, un 503, un desafío anti-bot, dos 403 seguidos o tres errores de red seguidos, se abandona el host por el resto de la corrida y se registra el motivo |
| robots.txt | Se descarga y se registra si cada URL estaría permitida; su `Crawl-delay` se respeta si supera el intervalo |
| Horario | Preferentemente fuera del horario laboral |

Con estos límites, una corrida de 40 repositorios hace como mucho 1.600 pedidos y tarda alrededor de una hora.

## Qué hace

**Entrada:** un CSV de repositorios (`id`, `nombre`, `url_base` y, si se conoce, `oai_endpoint`) y, opcionalmente, un CSV de ítems (`repo_id`, `url`). Si no hay ítems, toma 3 por repositorio de la primera página de `ListRecords` en `oai_dc`, usando las URLs de `dc:identifier`.

**Por repositorio**, en orden:

1. **Reconocimiento** (unos 6 pedidos): página de inicio (meta `generator` y señales anti-bot en encabezados y cuerpo), `robots.txt`, `Identify` y `ListMetadataFormats` de OAI-PMH probando las rutas típicas de cada plataforma, y sondeo de la REST de DSpace (`/server/api` en 7+, `/rest/status` en 5 y 6).
2. **Métodos por ítem:** prueba todos los aplicables, no solo hasta que uno funcione, para poder compararlos.
   - Directo: la URL ya devuelve un archivo.
   - Página: `citation_pdf_url` y enlaces con patrones conocidos (`/bitstream/`, `/bitstreams/…/download`, `/article/download/` de OJS, documentos de EPrints).
   - OAI: `GetRecord` en `xoai`, `ore` y `mets`, si el repositorio los ofrece; `xoai` indica el bundle de cada archivo.
   - REST de DSpace: `/rest/handle/…?expand=bitstreams` en 5 y 6; `pid/find`, bundles y bitstreams en 7+.
   - DOI o handle: sigue la redirección, registra el destino y aplica el método de página allí.
3. **Verificación:** cada URL candidata a archivo se pide una sola vez, aunque la hayan encontrado varios métodos, y se registran código, `Content-Type`, `Content-Length`, nombre y magic bytes.

Si se alcanza el tope de pedidos, lo que falta se marca como no probado. Un certificado TLS inválido se registra y el pedido se repite una vez sin verificar, única excepción a la regla de no reintentar.

## Qué deja

| Archivo | Contenido |
| --- | --- |
| `pedidos.jsonl` | Una línea por pedido: inicio, fin, host, repositorio, ítem, paso, método, URL, código, milisegundos hasta los encabezados, bytes, error y señal anti-bot. Es la fuente de todo lo demás |
| `repositorios.csv` | Una fila por repositorio: plataforma, versión y de dónde se obtuvo, endpoint y formatos OAI, REST disponible, anti-bot, TLS inválido, latencia mediana y p90, hosts alcanzados, pedidos y motivo de abandono |
| `resultados.csv` | Una fila por ítem y método: tipo de URL, resultado (ok, sin archivos, error, anti-bot, no aplica o no probado), pedidos, archivos encontrados y verificados, tipos y bundles |
| `capturas/` | Respuestas crudas por host, con encabezados y cuerpo truncado, para reprocesarlas sin volver a pedir y como base de un futuro simulador |
| `resumen.md` | Generado a partir de lo anterior, con una tabla por cada pregunta de la primera sección |

Un comando aparte verifica sobre `pedidos.jsonl` que nunca hubo dos pedidos simultáneos al mismo host y que se respetó el intervalo.

## Notas de implementación

- Python 3.12 con asyncio y httpx (HTTP/1.1 y `follow_redirects=False`, para manejar las redirecciones a mano), lxml o selectolax para HTML y XML, typer para la línea de comandos y pytest.
- Toda la cortesía vive en un único componente: un turno por host, con un lock y el instante en que terminó su último pedido, por el que pasa cada pedido, incluidos los saltos de una redirección. Ningún otro código hace pedidos por su cuenta.
- Los detectores (plataforma, versión, anti-bot) y los extractores de cada método son funciones puras sobre una respuesta ya obtenida, separadas de quien hace los pedidos. Así se prueban con las capturas y se pueden reutilizar después.
- Ctrl+C corta enseguida sin dejar pedidos colgados y, al relanzar, se saltean los repositorios ya terminados.
- Tres comandos: `relevar`, `resumir` y `verificar-cortesia`.
- Antes de la corrida completa, probar con dos o tres repositorios y revisar `pedidos.jsonl` a mano.

## Anexo: tamaño de los archivos (`medir-tamanos`, nov 2026)

Pedido por el usuario para calibrar el volumen de la prueba de concurrencia: los pilotos vieron 3 ítems por repositorio, y el tamaño medio de archivo salía de 51 archivos. Este comando pide páginas enteras de `ListRecords` en METS (o xoai) de los repositorios de los pilotos que publican el tamaño de cada archivo, sin descargar ningún archivo.

- Por repositorio: `robots.txt`, una página sin fechas (que trae `completeListSize`, la cantidad de ítems del repositorio) y una por ventana de fechas (hasta 2012, 2013–2016, 2017–2019, 2020–2022, desde 2023), para repartir la muestra en el tiempo.
- Cortesía: 10 s entre pedidos a un host (o el doble de la última respuesta), 2 hosts a la vez y 8 pedidos por repositorio. Las páginas siguen cortadas en 2 MB.
- Solo cuentan los archivos de contenido (sin los bundles THUMBNAIL, TEXT, LICENSE ni CC-LICENSE).
- Deja `tamanos.csv` (un archivo por fila) y `tamanos.json`: por repositorio y en total, ponderado por la cantidad de ítems de cada repositorio (y sin ponderar, como control).

## Anexo: muestra de repositorios de LA Referencia (`muestra-*`, nov 2026)

Pedido por el usuario para repetir la prueba de concurrencia contra repositorios reales en vez del simulador: una muestra de unos 150 repositorios útiles del listado de LA Referencia (`entradas/la_referencia_repos.csv`: 2.545 repositorios, 6,13 M documentos), con su país, su endpoint OAI e ítems candidatos con sus archivos. Solo metadatos: no se descarga ningún archivo.

- **Estratos por documentos**, con sorteo de semilla fija: 50.000 o más y 10.000–50.000, todos (censo); 1.000–10.000, 50; 100–1.000, 25; menos de 100, ninguno (suelen estar caídos y suman el 0,3 % de los documentos). En los estratos con cupo se sortean otras tantas reservas, que reemplazan en orden a los que no sirven (sin registro en el buscador, sin OAI, anti-bot, robots o abandonados). El motivo de cada descarte se informa: la tasa de repositorios inaccesibles también es un resultado.
- **Ubicación (`muestra-ubicar`):** una consulta por repositorio a la API del buscador de LA Referencia (`buscador.lareferencia.info/api/v1/search`, filtro `reponame_str`, `rawData`, 10 registros). Da el país (`network_acronym_str`), el namespace del identificador OAI (`oai:<namespace>:…`, que en DSpace es el host) y las URLs de los ítems. Se vota entre los registros, porque uno solo puede apuntar a un sitio externo: primero la revista OJS (o el sitio, si son varias revistas), después el host de las páginas de ítem del propio repositorio, el namespace y un host que se repita; la otra opción queda como alternativa. Si solo hay handles (los de España llegan con `oai:dnet:…`), se resuelve uno con la API de handle.net. Los pedidos van de a uno.
- **Cosecha (`muestra-cosechar`):** el reconocimiento de `relevar` (inicio, robots, `Identify` en las rutas típicas, `ListMetadataFormats`, REST de DSpace) desde la URL base, o desde el endpoint ya verificado en los pilotos. Si no aparece el OAI, se prueban `/oai2d` (Invenio), `/oai/scielo-oai.php` (SciELO) y `/oai`, y después el reconocimiento desde la URL alternativa. Se marca si el endpoint coincide con el namespace. Después, páginas de `ListRecords` en el formato más rico disponible (METS, xoai, ORE u `oai_dc`): un repositorio de hasta 600 ítems se cosecha entero siguiendo el `resumptionToken`; uno más grande, la primera página y una por ventana de fechas. De cada ítem se guardan la página, y de cada archivo de contenido la URL, el tamaño, el tipo y el bundle (sin THUMBNAIL, TEXT ni LICENSE). En `oai_dc` solo se toman los identificadores que ya apuntan a un archivo.
- **Cortesía:** 5 s entre pedidos a un host (o el doble de la última respuesta), 8 hosts a la vez, 20 pedidos por repositorio y páginas cortadas en 2 MB.
- **Resultados (`muestra-resumir`):** `repositorios.csv` (estrato, país, plataforma, endpoint, formato, estado y motivo), `items.csv`, `archivos.csv` y `resumen.json`/`resumen.md`: por estrato, y archivos por ítem y tamaño ponderados por documento y por repositorio (cada útil representa a `población del estrato ÷ útiles del estrato`).
- **Reintentos y robots (pedido por el usuario, nov 2026):** `--rehacer-fallidos` vuelve a cosechar, en otra corrida, los repositorios que fallaron por algo que puede cambiar (todos menos los de anti-bot), con presupuesto de pedidos propio y sin el abandono anterior de su host; `verificar-cortesia` lo acepta solo para esos ids (`…​.rN`) y en otra corrida. Con `--ignorar-robots`, lo que `robots.txt` prohíbe queda registrado pero no corta la cosecha (el `Crawl-delay` se sigue respetando). Si el endpoint encontrado no lista nada, se prueban los contextos `snrd`, `openaire` y `driver` de DSpace y los endpoints publicados en ROAR (`muestra-roar`, sobre `roar.eprints.org/rawlist.xml`). `muestra-elegir --ampliar C=…,D=…` suma candidatos sin cambiar los anteriores.
- **Ampliación (`muestra-ampliar`, pedido por el usuario, nov 2026):** la cosecha deja ~250 ítems por repositorio (una página por ventana de fechas en los grandes), que alcanzan para ~15 minutos de la prueba de concurrencia a carga plena. Para los repositorios útiles cosechados por ventanas, se vuelve a pedir la primera página de cada ventana y se sigue su `resumptionToken`, de a una página por ventana por turno (la muestra sigue repartida en el tiempo), hasta 2.000 ítems por repositorio; una ventana se deja tras dos páginas seguidas sin ítems nuevos. Cortesía: 10 s entre pedidos a un host (o el doble de la última respuesta), 6 hosts a la vez y 40 pedidos por repositorio, páginas cortadas en 2 MB. Queda en `muestra/ampliacion/` (pedidos y eventos propios, que `verificar-cortesia --ampliacion` controla) y `muestra-resumir` agrega esos ítems después de los de la cosecha.
