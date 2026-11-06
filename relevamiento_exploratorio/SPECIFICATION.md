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
