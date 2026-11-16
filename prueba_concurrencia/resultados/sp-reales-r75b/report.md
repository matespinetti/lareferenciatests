# Prueba de concurrencia contra repositorios reales

## Instancia

- **tipo:** t4g.large
- **zona:** sa-east-1a
- **vcpu:** 2
- **memoria_mb:** 7802
- **kernel:** 7.0.0-1014-aws
- **docker:** 29.1.3
- **repositorios:** reales: muestra de LA Referencia
- **s3:** s3://prueba-concurrencia-mateo-sp
- **carga_web:** 450 MB, 0.1 núcleo


## Repositorios reales

El motor contra la muestra de LA Referencia (/app/muestra): cada paso baja una parte distinta de los ítems de cada repositorio, con 2 s de cortesía por repositorio (un pedido a la vez), `Retry-After` respetado y abandono ante un 403, un `Retry-After` de más de 5 minutos o tres errores de red seguidos. Tope de 1 GB conocidos por repositorio y paso. La cortesía la verifica el motor sobre su propio registro de pedidos: no hay un servidor que la cuente.

| Corrutinas | Repos en el paso | Con trabajo (media) | MB/s bajados | MB/s subidos | MB/s por corrutina | Ítems/s | Resoluciones/s | Descargas/s | Errores | 429/503 | Abandonados | CPU máquina | Memoria libre mín. | Bucle p99 | Cortesía |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 75 | 266 | 118,4 | 36,6 | 36,2 | 0,49 | 18,8 | 15,2 | 24,1 | 606 | 36/37 | 29 | 44,5 % (base 30%) | 5.942 MB | 34,7 ms | cumple |

### Velocidad real por conexión

Descargas correctas de 1 MB o más, desde la primera respuesta hasta el último byte. El simulador suponía una mediana de 3.30 MB/s.

| Corrutinas | Descargas | p25 | Mediana | p75 | Latencia p50 | Latencia p95 |
|---|---|---|---|---|---|---|
| 75 | 3745 | 1,36 | 3,17 | 6,03 | 301 ms | 1.498 ms |

### Errores y abandonos

- **75 corrutinas:** errores: http_404 256, ClientConnectorError 102, http_301 100, http_403 71, ConnectionTimeoutError 45, SocketTimeoutError 18, http_500 6, TooManyRedirects 4, ClientConnectorDNSError 3, http_502 1. Abandonados: red_ConnectionTimeoutError 10, red_ClientConnectorError 10, http_403 5, red_SocketTimeoutError 3, red_ClientConnectorDNSError 1.

Ejemplos por tipo de error (hosts distintos):

| Error | Tipo | Hosts | URL | Detalle |
|---|---|---|---|---|
| http_404 | descarga | 19 | `https://acervodigital.ufpr.br/bitstream/1884/68224/1/R%20-%20E%20-%20JONATHAN%20GROCHOVSKI%20DA%20SI` | tipo=text/html;charset=utf-8 |
| http_404 | descarga | 19 | `https://dspace.sti.ufcg.edu.br/bitstream/riufcg/113/3/DAVI+OLIVEIRA+SERRANO+DE+ANDRADE+-+DISSERTA%C3` | tipo=text/html;charset=UTF-8 |
| http_404 | descarga | 19 | `http://sgc.anlis.gob.ar//bitstream/123456789/34/1/MEDICINA%20%28Buenos%20Aires%29%201999%20-%2059%20` | tipo=text/html; charset=iso-8859-1 |
| ClientConnectorError | descarga | 12 | `http://localhost:8080/jspui/bitstream/44000/23/1/T-ULVR-0042.pdf` |  |
| ClientConnectorError | descarga | 12 | `https://repositorioinstitucional.uabc.mx/bitstreams/5085263d-fde7-4310-9470-210e8271600f/download` |  |
| ClientConnectorError | resolucion | 12 | `https://culturales.uabc.mx/index.php/Culturales/article/view/6` |  |
| ConnectionTimeoutError | descarga | 12 | `http://www.bdtd.uerj.br/bitstream/1/3921/1/Dissertacao+Mayara+Pacheco+completa.pdf` |  |
| ConnectionTimeoutError | descarga | 12 | `https://repositorio.upse.edu.ec/bitstreams/de6ce64a-dba3-4127-8a84-3d35e097970f/download` |  |
| ConnectionTimeoutError | descarga | 12 | `http://172.24.0.86//bitstreams/6b7d7a4d-ddc8-435e-bf57-8f2f98c97cc0/download` |  |
| ClientConnectorCertificateError | descarga | 9 | `https://acervodigital.ufpr.br/bitstream/1884/68224/1/R%20-%20E%20-%20JONATHAN%20GROCHOVSKI%20DA%20SI` |  |
| ClientConnectorCertificateError | descarga | 9 | `https://bcnroc.ajuntament.barcelona.cat/bitstreams/2a2e7b2c-2570-4758-bdb0-fc150d0ebb0a/download` |  |
| ClientConnectorCertificateError | descarga | 9 | `http://repositorio.cfe.edu.uy/bitstream/123456789/652/2/Varela%2cM.Teenage.pdf` |  |
| SocketTimeoutError | resolucion | 8 | `https://periodicos.uninove.br/riae/article/view/10248` |  |
| SocketTimeoutError | resolucion | 8 | `https://decires.cepe.unam.mx/index.php/decires/article/view/4` |  |
| SocketTimeoutError | descarga | 8 | `https://repositorio.ufopa.edu.br/bitstreams/20928b04-bef1-4436-b7ea-bcb2a8f3aea2/download` |  |
| http_403 | descarga | 7 | `https://repositorio.ufrn.br/bitstreams/cf10a5cd-fdbd-48fe-95a0-9f65139940c6/download` | tipo=text/html |
| http_403 | descarga | 7 | `https://upcommons.upc.edu/bitstreams/102d09a8-a70e-44b3-879a-decf05abe30c/download` | tipo=text/html; charset=utf-8 |
| http_403 | descarga | 7 | `https://revistas.ufpr.br/educar/article/download/2028/30581` | tipo=text/html |
| http_500 | descarga | 4 | `https://repositorioinstitucional.uabc.mx/bitstreams/9e2f6a9a-b061-4617-94b0-532cd243f482/download` | tipo=text/html |
| http_500 | descarga | 4 | `https://periodicos.ufsm.br/animus/article/download/2787/2114` | tipo=text/html; charset=UTF-8 |
| http_500 | descarga | 4 | `https://repositorio.ufpa.br/bitstreams/132635bc-64f8-4a26-94c1-25de8ba97252/download` | tipo=application/json;charset=UTF-8 |
| http_301 | descarga | 3 | `https://bcnroc.ajuntament.barcelona.cat/bitstreams/2a2e7b2c-2570-4758-bdb0-fc150d0ebb0a/download` | location=(sin Location) |
| http_301 | descarga | 3 | `https://app.uff.br/riuff/bitstream/1/39183/1/INVENT%c3%81RIO%20DE%20GASES%20DE%20EFEITO%20ESTUFA%20D` | location=(sin Location) |
| http_301 | descarga | 3 | `https://udimundus.udima.es/bitstream/20.500.12226/2911/1/Ciberdelincuencia%20juvenil%20y%20Modelo%20` | location=(sin Location) |
| TooManyRedirects | descarga | 3 | `https://revistas.usp.br/aesalq/en/article/download/4835/6365` |  |
| TooManyRedirects | resolucion | 3 | `https://periodicos.ufersa.edu.br/index.php/caatinga/article/view/89` |  |
| TooManyRedirects | descarga | 3 | `https://periodicos.ufv.br/apgs/article/download/14059/7897` |  |
| ClientConnectorDNSError | descarga | 1 | `http://bibliodigitalnovo.unijui.edu.br:4000/bitstreams/eabcd306-16cb-4609-9b6b-3fb27a54f69f/download` |  |
| http_502 | descarga | 1 | `https://repositorio.unal.edu.co/bitstreams/26ed589c-f9b0-4f7e-8a78-5c1f437b7631/download` | tipo=text/html; charset=iso-8859-1 |

## Subida a S3

El motor guarda cada archivo mientras lo baja (los de menos de 8 MB en el spool en RAM, los demás en disco) y, al terminar, lo deja para un proceso subidor aparte (boto3, en su propio núcleo) que lo sube a S3 (real o, en los ensayos, un S3 falso local: la columna "Dónde sube"). Si lo pendiente pasa 256 MB en RAM o 4.096 MB en disco, el motor deja de repartir tareas nuevas ("frenado"). **Lo que importa: frenado en 0 y lo pendiente estable** quieren decir que la descarga nunca esperó a la subida.

| Esc. | Dónde sube | Lugares | Tope de red | MB/s bajados | MB/s subidos | CPU motor | CPU motor sin subida | RSS motor (MB) | CPU subidor (media / máx) | RSS subidor (MB) | Pendiente máx RAM / disco (MB) | Frenado (s) | Escritura a disco (MB/s) | Objetos (multipart) | Errores de subida | Pendiente al final (MB) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| r75-host | proceso aparte (16), S3 real | 75 | sin tope | 36,6 | 36,2 | 45,8 | — | 412 | 22,9 / 117,6 | 129 | 96 / 522 | 0,0 | 2,5 | 16598 (0) | 0 | 0 |

- **Dónde sube:** proceso aparte (en su propio núcleo), un pool de hilos con s3transfer dentro del proceso del motor, o un pool de corrutinas con aiobotocore dentro del bucle del motor. Entre paréntesis, las subidas a la vez. El spool es el mismo en los tres: la única diferencia es dónde corre la subida.
- **CPU motor contra la referencia sin subida:** en proceso aparte, lo que le agrega al motor escribir el spool; con hilos o corrutinas, además todo el costo de subir, que comparte el núcleo (y el GIL) con la descarga. Si la CPU llega al 100 %, bajan los MB/s bajados y sube el retraso del bucle.
- **CPU del subidor:** solo se ve aparte en el proceso aparte. Con el SDK CRT de AWS sería menos.
- **Escritura a disco:** de toda la máquina; es lo que tiene que sostener el volumen EBS del spool (gp3 da 125 MB/s de base) y el tope de EBS de la instancia.
- En corridas cortas pasan sobre todo los archivos chicos: los grandes no llegan a terminar de bajar, así que el spool en disco aparece menos de lo que se usaría en la descarga real.
