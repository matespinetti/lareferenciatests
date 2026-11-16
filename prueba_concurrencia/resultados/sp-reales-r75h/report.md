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

El motor contra la muestra de LA Referencia (/app/muestra): los ítems de cada repositorio, con 2 s de cortesía por repositorio (un pedido a la vez), `Retry-After` respetado y abandono ante un 403, un `Retry-After` de más de 5 minutos o tres errores de red seguidos. Tope de 1 GB conocidos por repositorio y paso. La cortesía la verifica el motor sobre su propio registro de pedidos: no hay un servidor que la cuente.

| Corrutinas | Repos en el paso | Con trabajo (media) | MB/s bajados | MB/s subidos | MB/s por corrutina | Ítems/s | Resoluciones/s | Descargas/s | Errores | 429/503 | Abandonados | CPU máquina | Memoria libre mín. | Bucle p99 | Cortesía |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 75 | 267 | 239,8 | 72,4 | 71,6 | 0,97 | 35,8 | 22,9 | 44,8 | 6998 | 490/187 | 31 | 75,3 % (base 30%) | 5.072 MB | 111,8 ms | cumple |

### Velocidad real por conexión

Descargas correctas de 1 MB o más, desde la primera respuesta hasta el último byte. El simulador suponía una mediana de 3.30 MB/s.

| Corrutinas | Descargas | p25 | Mediana | p75 | Latencia p50 | Latencia p95 |
|---|---|---|---|---|---|---|
| 75 | 39433 | 1,20 | 2,40 | 4,75 | 326 ms | 1.650 ms |

### Errores y abandonos

- **75 corrutinas:** errores: http_404 4679, http_301 1476, http_403 332, http_500 188, ClientConnectorError 108, http_502 70, SocketTimeoutError 66, ConnectionTimeoutError 46, TooManyRedirects 18, ServerDisconnectedError 7, ClientPayloadError 5, ClientConnectorDNSError 3. Abandonados: red_ConnectionTimeoutError 16, red_ClientConnectorError 7, http_403 4, red_SocketTimeoutError 3, red_ClientConnectorDNSError 1.

Ejemplos por tipo de error (hosts distintos):

| Error | Tipo | Hosts | URL | Detalle |
|---|---|---|---|---|
| http_404 | descarga | 32 | `https://acervodigital.ufpr.br/bitstream/1884/59445/1/Planejamento%20de%20comunicacao%20institucional` | tipo=text/html;charset=utf-8 |
| http_404 | descarga | 32 | `https://dspace.sti.ufcg.edu.br/bitstream/riufcg/110/3/DANIELLE+NUBIA+SOUZA+SANTOS+-+DISSERTA%C3%87%C` | tipo=text/html;charset=UTF-8 |
| http_404 | descarga | 32 | `http://sgc.anlis.gob.ar//bitstream/123456789/34/1/MEDICINA%20%28Buenos%20Aires%29%201999%20-%2059%20` | tipo=text/html; charset=iso-8859-1 |
| ConnectionTimeoutError | descarga | 18 | `http://repositorio2.unb.br/jspui/bitstream/10482/12039/1/ARTIGO_AnaliseProgramasTransferenciaRenda.p` |  |
| ConnectionTimeoutError | descarga | 18 | `http://www.bdtd.uerj.br/bitstream/1/3919/1/Maycon+Rogerio+Grigio.pdf` |  |
| ConnectionTimeoutError | descarga | 18 | `http://bdtd.uftm.edu.br/bitstream/tede/237/2/Dissert%20Aline%20O%20M%20Pires.pdf` |  |
| SocketTimeoutError | descarga | 16 | `https://arca.fiocruz.br/bitstreams/c2f8fa55-0b7a-4768-bb3c-8a24cea89507/download` |  |
| SocketTimeoutError | descarga | 16 | `https://e-spacio.uned.es/bitstreams/089d7323-fdc3-40f2-be06-98cf7ba0c5ea/download` |  |
| SocketTimeoutError | descarga | 16 | `https://digital.csic.es/bitstream/10261/1442/1/1471-2199-7-39.pdf` |  |
| http_500 | resolucion | 13 | `https://revistas.juridicas.unam.mx/index.php/derecho-comparado/article/view/542` | tipo=text/html; charset=UTF-8 |
| http_500 | descarga | 13 | `https://digibuo.uniovi.es/dspace/bitstream/10651/76767/1/A_Deep_Graph_Neural_Network_Approach_for_As` | tipo=text/html;charset=utf-8 |
| http_500 | descarga | 13 | `https://documenta.ciemat.es/bitstreams/09efbaec-e1e9-4cc8-95b0-66139ffb0241/download` | tipo=text/html; charset=utf-8 |
| ClientConnectorError | descarga | 12 | `http://localhost:8080/jspui/bitstream/44000/15/1/T-ULVR-0001.pdf` |  |
| ClientConnectorError | descarga | 12 | `https://repositorioinstitucional.uabc.mx/bitstreams/ccb445f8-9184-4b90-9ce2-b107a281e091/download` |  |
| ClientConnectorError | descarga | 12 | `https://cicese.repositorioinstitucional.mx/jspui/bitstream/1007/38/1/ebiblio_17453.pdf` |  |
| ClientConnectorCertificateError | descarga | 11 | `https://acervodigital.ufpr.br/bitstream/1884/59445/1/Planejamento%20de%20comunicacao%20institucional` |  |
| ClientConnectorCertificateError | descarga | 11 | `https://bcnroc.ajuntament.barcelona.cat/bitstreams/4536adf0-c70a-4c3d-a00b-4f7fffece041/download` |  |
| ClientConnectorCertificateError | descarga | 11 | `http://repositorio.cfe.edu.uy/bitstream/123456789/879/2/Evaluacion%20programa%20educacion%20sexual.p` |  |
| http_403 | descarga | 7 | `https://repositorio.ufrn.br/bitstreams/d34269c7-b799-4fe4-8059-d7fca2b23f84/download` | tipo=text/html |
| http_403 | descarga | 7 | `https://upcommons.upc.edu/bitstreams/ef768de6-da22-4f20-bf35-4a57149197ac/download` | tipo=text/html; charset=utf-8 |
| http_403 | descarga | 7 | `https://revistas.ufpr.br/educar/article/download/2026/30579` | tipo=text/html |
| http_502 | descarga | 6 | `https://repositorio.ipea.gov.br/bitstreams/96927325-fd03-4590-9b09-d9f914204732/download` | tipo=text/html; charset=iso-8859-1 |
| http_502 | descarga | 6 | `https://www.repositorio.ufop.br/bitstreams/4f1cd4ad-5eb4-4a35-8f03-d061758e7742/download` | tipo=text/html |
| http_502 | descarga | 6 | `https://diposit.ub.edu/bitstreams/0de43e48-2a69-4a30-a094-3dbd1365fd4e/download` | tipo=text/html; charset=iso-8859-1 |
| http_301 | descarga | 5 | `https://bcnroc.ajuntament.barcelona.cat/bitstreams/4536adf0-c70a-4c3d-a00b-4f7fffece041/download` | location=(sin Location) |
| http_301 | descarga | 5 | `https://udimundus.udima.es/bitstream/20.500.12226/1641/1/24.%20Publicacio%cc%81n%20Congreso%20Vigo.p` | location=(sin Location) |
| http_301 | descarga | 5 | `http://www.riarte.es/bitstream/20.500.12251/3499/1/R043.%20Landing%20page%20%e2%80%9cNuevos%20tiempo` | location=(sin Location) |
| ServerDisconnectedError | descarga | 5 | `https://burjcdigital.urjc.es/bitstreams/41f5b604-3cbb-4e58-937d-defd8fe2a2d7/download` |  |
| ServerDisconnectedError | descarga | 5 | `http://bu.furb.br/docs/TE/2005/305948_1_1.pdf` |  |
| ServerDisconnectedError | resolucion | 5 | `https://periodicos.ufba.br/index.php/revistaoes/article/view/11789` |  |
| ClientPayloadError | descarga | 3 | `http://repositorio.cfe.edu.uy/bitstream/123456789/1542/2/Ceibal%20%20%20III%20Seminario%20de%20Tecno` |  |
| ClientPayloadError | descarga | 3 | `https://riunet.upv.es/bitstreams/f364fc1b-bca3-4295-a5e3-ce0106f33cad/download` |  |
| ClientPayloadError | descarga | 3 | `https://burjcdigital.urjc.es/bitstreams/379bb2cf-77bc-4764-8da9-c665b33b1eae/download` |  |
| TooManyRedirects | resolucion | 2 | `https://periodicos.ufersa.edu.br/index.php/caatinga/article/view/93` |  |
| TooManyRedirects | descarga | 2 | `https://periodicos.ufv.br/apgs/article/download/5396/5180` |  |
| ClientConnectorDNSError | descarga | 1 | `http://bibliodigitalnovo.unijui.edu.br:4000/bitstreams/eabcd306-16cb-4609-9b6b-3fb27a54f69f/download` |  |

## Corrida larga: 75 corrutinas, 60 minutos (r75h-host)

Todos los ítems de la muestra en una sola corrida. Un repositorio que termina sus ítems vuelve a empezar (vueltas de al menos 10 minutos), así hay trabajo toda la hora. Vueltas terminadas: 43 repositorios con 0, 61 repositorios con 1, 49 repositorios con 2, 27 repositorios con 3, 18 repositorios con 4, 10 repositorios con 5, 56 repositorios con 6, 3 repositorios con 7.

### Cada 5 minutos

Los errores salen del registro de pedidos: si suben con el tiempo, los repositorios empiezan a bloquear.

| Minutos | MB/s bajados | MB/s subidos | Repos con trabajo | Ocupación | CPU máquina | Memoria libre mín. | RSS motor | Pedidos | 403 | 429/503 | Errores de red |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0–5 | 72,4 | 73,9 | 245 | 98 % | 74,3 % | 5.784 MB | 504 MB | 21126 | 39 | 33 | 28 |
| 5–10 | 63,1 | 60,3 | 243 | 92 % | 69,6 % | 5.749 MB | 566 MB | 19883 | 41 | 75 | 16 |
| 10–15 | 81,1 | 81,1 | 242 | 97 % | 79,0 % | 5.470 MB | 642 MB | 21096 | 20 | 33 | 19 |
| 15–20 | 69,3 | 68,5 | 242 | 92 % | 72,9 % | 5.443 MB | 657 MB | 19886 | 1 | 35 | 10 |
| 20–25 | 77,8 | 76,7 | 242 | 99 % | 77,3 % | 5.406 MB | 741 MB | 19481 | 18 | 14 | 20 |
| 25–30 | 69,9 | 67,2 | 241 | 91 % | 73,8 % | 5.451 MB | 758 MB | 20359 | 46 | 63 | 10 |
| 30–35 | 82,5 | 83,0 | 239 | 95 % | 80,8 % | 5.383 MB | 806 MB | 21307 | 6 | 57 | 14 |
| 35–40 | 67,1 | 65,4 | 239 | 90 % | 73,6 % | 5.177 MB | 836 MB | 19497 | 51 | 73 | 18 |
| 40–45 | 81,1 | 80,7 | 236 | 96 % | 80,2 % | 5.072 MB | 884 MB | 20874 | 69 | 76 | 17 |
| 45–50 | 64,8 | 63,8 | 236 | 86 % | 73,0 % | 5.281 MB | 907 MB | 20117 | 28 | 87 | 14 |
| 50–55 | 73,0 | 71,8 | 236 | 96 % | 76,3 % | 5.313 MB | 950 MB | 20836 | 2 | 56 | 18 |
| 55–60 | 67,1 | 66,3 | 236 | 88 % | 72,8 % | 5.192 MB | 977 MB | 19155 | 2 | 75 | 9 |

### Primera vuelta contra las siguientes

Si las vueltas siguientes bajan más rápido, parte de la velocidad es caché del repositorio (o de su CDN) y no vale para la descarga real, que pide cada archivo una sola vez.

| Vuelta | Pedidos | Correctos | Descargas de 1 MB o más | MB/s por conexión (mediana) | p25 | p75 |
|---|---|---|---|---|---|---|
| 1ª | 108780 | 97,9 % | 14397 | 2,60 | 1,22 | 4,99 |
| 2ª | 69685 | 97,7 % | 9325 | 2,49 | 1,24 | 4,83 |
| 3ª o más | 68630 | 94,5 % | 15711 | 2,20 | 1,15 | 4,46 |

## Subida a S3

El motor guarda cada archivo mientras lo baja (los de menos de 8 MB en el spool en RAM, los demás en disco) y, al terminar, lo deja para un proceso subidor aparte (boto3, en su propio núcleo) que lo sube a S3 (real o, en los ensayos, un S3 falso local: la columna "Dónde sube"). Si lo pendiente pasa 256 MB en RAM o 4.096 MB en disco, el motor deja de repartir tareas nuevas ("frenado"). **Lo que importa: frenado en 0 y lo pendiente estable** quieren decir que la descarga nunca esperó a la subida.

| Esc. | Dónde sube | Lugares | Tope de red | MB/s bajados | MB/s subidos | CPU motor | CPU motor sin subida | RSS motor (MB) | CPU subidor (media / máx) | RSS subidor (MB) | Pendiente máx RAM / disco (MB) | Frenado (s) | Escritura a disco (MB/s) | Objetos (multipart) | Errores de subida | Pendiente al final (MB) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| r75h-host | proceso aparte (16), S3 real | 75 | sin tope | 72,4 | 71,6 | 82,0 | — | 977 | 48,4 / 129,8 | 134 | 120 / 1.051 | 0,0 | 5,8 | 156397 (0) | 0 | 0 |

- **Dónde sube:** proceso aparte (en su propio núcleo), un pool de hilos con s3transfer dentro del proceso del motor, o un pool de corrutinas con aiobotocore dentro del bucle del motor. Entre paréntesis, las subidas a la vez. El spool es el mismo en los tres: la única diferencia es dónde corre la subida.
- **CPU motor contra la referencia sin subida:** en proceso aparte, lo que le agrega al motor escribir el spool; con hilos o corrutinas, además todo el costo de subir, que comparte el núcleo (y el GIL) con la descarga. Si la CPU llega al 100 %, bajan los MB/s bajados y sube el retraso del bucle.
- **CPU del subidor:** solo se ve aparte en el proceso aparte. Con el SDK CRT de AWS sería menos.
- **Escritura a disco:** de toda la máquina; es lo que tiene que sostener el volumen EBS del spool (gp3 da 125 MB/s de base) y el tope de EBS de la instancia.
- En corridas cortas pasan sobre todo los archivos chicos: los grandes no llegan a terminar de bajar, así que el spool en disco aparece menos de lo que se usaría en la descarga real.
