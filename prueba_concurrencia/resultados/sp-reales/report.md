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
| 25 | 267 | 196,7 | 23,1 | 23,9 | 0,92 | 6,4 | 1,4 | 8,1 | 348 | 0/21 | 23 | 31,5 % (base 30%) | 6.288 MB | 11,8 ms | cumple |
| 50 | 267 | 167,0 | 26,7 | 26,4 | 0,53 | 14,2 | 10,6 | 17,6 | 497 | 0/0 | 35 | 38,2 % (base 30%) | 6.246 MB | 20,1 ms | cumple |
| 75 | 266 | 139,8 | 31,8 | 31,5 | 0,42 | 17,0 | 13,5 | 21,7 | 561 | 49/123 | 44 | 43,7 % (base 30%) | 5.998 MB | 36,9 ms | cumple |
| 120 | 266 | 120,1 | 31,2 | 31,3 | 0,26 | 16,6 | 13,0 | 21,4 | 558 | 31/47 | 48 | 44,2 % (base 30%) | 6.054 MB | 36,2 ms | cumple |

### Velocidad real por conexión

Descargas correctas de 1 MB o más, desde la primera respuesta hasta el último byte. El simulador suponía una mediana de 3.30 MB/s.

| Corrutinas | Descargas | p25 | Mediana | p75 | Latencia p50 | Latencia p95 |
|---|---|---|---|---|---|---|
| 25 | 1673 | 0,85 | 1,99 | 4,06 | 441 ms | 1.662 ms |
| 50 | 2730 | 1,07 | 2,22 | 4,80 | 320 ms | 2.301 ms |
| 75 | 3238 | 1,11 | 2,52 | 5,10 | 303 ms | 2.374 ms |
| 120 | 3461 | 1,16 | 2,43 | 4,89 | 333 ms | 2.807 ms |

### Errores y abandonos

- **25 corrutinas:** errores: http_404 168, http_301 84, ClientConnectorError 33, ConnectionTimeoutError 28, SocketTimeoutError 13, ServerDisconnectedError 7, http_500 6, ClientConnectorDNSError 5, http_403 3, TooManyRedirects 1. Abandonados: red_ConnectionTimeoutError 9, red_ClientConnectorError 9, http_403 3, red_SocketTimeoutError 1, red_ClientConnectorDNSError 1.
- **50 corrutinas:** errores: http_404 229, http_301 92, ClientConnectorError 42, ConnectionTimeoutError 42, ClientConnectorDNSError 21, TooManyRedirects 19, http_500 14, SocketTimeoutError 14, http_403 12, http_468 4, ServerDisconnectedError 2, ClientConnectorSSLError 2, http_504 1, http_410 1, ClientOSError 1, http_522 1. Abandonados: http_403 12, red_ConnectionTimeoutError 10, red_ClientConnectorError 10, red_ClientConnectorDNSError 2, red_SocketTimeoutError 1.
- **75 corrutinas:** errores: http_404 247, http_301 100, ConnectionTimeoutError 55, ClientConnectorError 50, http_403 23, ClientConnectorDNSError 21, http_500 19, TooManyRedirects 17, SocketTimeoutError 13, ServerDisconnectedError 5, http_468 4, http_502 4, ClientPayloadError 1, http_401 1, http_410 1. Abandonados: http_403 23, red_ClientConnectorError 10, red_ConnectionTimeoutError 8, red_SocketTimeoutError 1, red_ServerDisconnectedError 1, red_ClientConnectorDNSError 1.
- **120 corrutinas:** errores: http_404 250, http_301 87, ConnectionTimeoutError 58, ClientConnectorError 53, http_403 27, http_500 27, ClientConnectorDNSError 19, SocketTimeoutError 10, http_468 7, ServerDisconnectedError 6, http_502 4, TooManyRedirects 3, ClientOSError 2, http_202 1, http_400 1, http_522 1, ClientPayloadError 1, ClientConnectorSSLError 1. Abandonados: http_403 27, red_ConnectionTimeoutError 9, red_ClientConnectorError 9, red_SocketTimeoutError 1, red_ServerDisconnectedError 1, red_ClientConnectorDNSError 1.

Ejemplos por tipo de error (hosts distintos):

| Error | Tipo | Hosts | URL | Detalle |
|---|---|---|---|---|
| http_404 | descarga | 101 | `https://acervodigital.ufpr.br/bitstream/1884/59445/1/Planejamento%20de%20comunicacao%20institucional` | tipo=text/html;charset=utf-8 |
| http_404 | descarga | 101 | `https://dspace.sti.ufcg.edu.br/bitstream/riufcg/110/3/DANIELLE+NUBIA+SOUZA+SANTOS+-+DISSERTA%C3%87%C` | tipo=text/html;charset=UTF-8 |
| http_404 | descarga | 101 | `http://sgc.anlis.gob.ar//bitstream/123456789/35/1/Medicina%20%28Buenos%20Aires%29%20vol57%2cn4%2c199` | tipo=text/html; charset=iso-8859-1 |
| ClientConnectorDNSError | descarga | 45 | `http://bibliodigitalnovo.unijui.edu.br:4000/bitstreams/eff092dc-3c43-4c21-9aa3-29ab0fe56593/download` |  |
| ClientConnectorDNSError | descarga | 45 | `http://dspace.usc.es/bitstream/10347/4587/1/pg_375-388_semata16.pdf` |  |
| ClientConnectorDNSError | descarga | 45 | `http://www6.univali.br/seer/index.php/nej/article/download/1225/1028` |  |
| ConnectionTimeoutError | descarga | 37 | `http://www.bdtd.uerj.br/bitstream/1/3919/1/Maycon+Rogerio+Grigio.pdf` |  |
| ConnectionTimeoutError | descarga | 37 | `http://repositorio2.unb.br/jspui/bitstream/10482/12039/1/ARTIGO_AnaliseProgramasTransferenciaRenda.p` |  |
| ConnectionTimeoutError | descarga | 37 | `https://repositorio.upse.edu.ec/bitstreams/bc26297e-ac98-44d0-bfc4-8340d6fa0d3b/download` |  |
| http_403 | descarga | 32 | `https://repositorio.ufrn.br/bitstreams/d34269c7-b799-4fe4-8059-d7fca2b23f84/download` | tipo=text/html |
| http_403 | descarga | 32 | `https://upcommons.upc.edu/bitstreams/ef768de6-da22-4f20-bf35-4a57149197ac/download` | tipo=text/html; charset=utf-8 |
| http_403 | descarga | 32 | `https://revistas.ufpr.br/educar/article/download/2026/30579` | tipo=text/html |
| ClientConnectorCertificateError | descarga | 31 | `https://acervodigital.ufpr.br/bitstream/1884/59445/1/Planejamento%20de%20comunicacao%20institucional` |  |
| ClientConnectorCertificateError | descarga | 31 | `http://repositorio.cfe.edu.uy/bitstream/123456789/879/2/Evaluacion%20programa%20educacion%20sexual.p` |  |
| ClientConnectorCertificateError | descarga | 31 | `https://bcnroc.ajuntament.barcelona.cat/bitstreams/4536adf0-c70a-4c3d-a00b-4f7fffece041/download` |  |
| ClientConnectorError | descarga | 28 | `http://localhost:8080/jspui/bitstream/44000/15/1/T-ULVR-0001.pdf` |  |
| ClientConnectorError | descarga | 28 | `https://repositorioinstitucional.uabc.mx/bitstreams/6c1355d4-8786-4d02-b643-090691ea8361/download` |  |
| ClientConnectorError | descarga | 28 | `https://cicese.repositorioinstitucional.mx/jspui/bitstream/1007/94/1/TESIS+LUIS+MARQUEZ+01_nov_2016.` |  |
| http_500 | descarga | 13 | `https://inaoe.repositorioinstitucional.mx/jspui/bitstream/1009/9/1/LaraRoLD.pdf` | tipo=text/html |
| http_500 | descarga | 13 | `https://repositorioinstitucional.uabc.mx/bitstreams/ccb445f8-9184-4b90-9ce2-b107a281e091/download` | tipo=text/html |
| http_500 | descarga | 13 | `https://cicese.repositorioinstitucional.mx/jspui/bitstream/1007/38/1/ebiblio_17453.pdf` | tipo=text/html |
| SocketTimeoutError | descarga | 12 | `https://repositorio.uam.es/bitstreams/71af987f-17ee-4d05-be33-b1f70ff14a64/download` |  |
| SocketTimeoutError | descarga | 12 | `https://diposit.ub.edu/bitstreams/32830d32-1e06-4101-bae8-377b23c8fedf/download` |  |
| SocketTimeoutError | descarga | 12 | `http://ridaa.unq.edu.ar/bitstream/20.500.11807/153/1/Alba+SIlot.pdf` |  |
| http_301 | descarga | 5 | `https://bcnroc.ajuntament.barcelona.cat/bitstreams/4536adf0-c70a-4c3d-a00b-4f7fffece041/download` | location=(sin Location) |
| http_301 | descarga | 5 | `http://www.riarte.es/bitstream/20.500.12251/929/1/L%c2%b4INFORMATIU%20332%20MAYO%202012.%20pp.%2076-` | location=(sin Location) |
| http_301 | descarga | 5 | `https://repositori.uvic-ucc.cat/bitstream/10854/181014/1/artconlli_a2022_khan_sarah_an_EMI_lecturer%` | location=(sin Location) |
| ServerDisconnectedError | descarga | 5 | `https://burjcdigital.urjc.es/bitstreams/3301f012-5a9d-4e6c-beab-8124f2570f44/download` |  |
| ServerDisconnectedError | descarga | 5 | `http://www.riarte.es/bitstream/20.500.12251/520/1/CERCHA%20124%20ABRIL%2015.%20pp.%2018-25.%20Rehabi` |  |
| ServerDisconnectedError | descarga | 5 | `https://e-spacio.uned.es/bitstreams/d5830f84-70dc-4c57-be06-810edcac3d9b/download` |  |
| TooManyRedirects | resolucion | 4 | `https://periodicos.ufersa.edu.br/index.php/caatinga/article/view/93` |  |
| TooManyRedirects | descarga | 4 | `https://revistas.usp.br/ts/article/download/12292/14069` |  |
| TooManyRedirects | descarga | 4 | `https://periodicos.ufv.br/apgs/article/download/5396/5180` |  |
| http_468 | descarga | 4 | `https://repositorio.ufba.br/bitstream/ri/20727/1/D%C3%A9bora%20Sampaio%20Leit%C3%A3o%20-%20DISSERTAC` | tipo=text/html |
| http_468 | descarga | 4 | `https://tede2.pucsp.br/bitstream/handle/10072/1/Tania%20Rossi%20Garbin.pdf` | tipo=text/html |
| http_468 | descarga | 4 | `https://sapientia.pucsp.br/bitstream/handle/20566/2/Maria%20Emiliana%20Lima%20Penteado.pdf` | tipo=text/html |
| http_502 | descarga | 4 | `https://lume.ufrgs.br/bitstream/handle/10183/204269/001109219.pdf?sequence=1&isAllowed=y` | tipo=text/html |
| http_502 | descarga | 4 | `https://periodicos.ufpe.br/revistas/revistageografia/article/download/228730/23144` | tipo=text/html; charset=utf-8 |
| http_502 | descarga | 4 | `https://www.lume.ufrgs.br/bitstream/handle/10183/95149/000917575.pdf?sequence=1` | tipo=text/html |
| ClientOSError | descarga | 3 | `http://proxy.furb.br/ojs/index.php/rea/article/download/4753/2998` |  |
| ClientOSError | descarga | 3 | `http://tede2.uefs.br:8080/bitstream/tede/363/2/DISSERTA%C3%87%C3%83O%20CARDOSO%2C%20MARIANA.pdf` |  |
| ClientOSError | descarga | 3 | `http://revista.cade.gov.br/index.php/revistadedefesadaconcorrencia/article/download/292/151` |  |
| http_522 | descarga | 2 | `http://www.revistasaludmental.mx/index.php/salud_mental/article/download/667/666` | tipo=text/html; charset=UTF-8 |
| http_522 | descarga | 2 | `https://repositorio.uesiglo21.edu.ar/bitstream/handle/ues21/17976/ODESSER%20ROBERTO%20AGUSTIN.pdf?se` | tipo=text/html; charset=UTF-8 |
| ClientConnectorSSLError | descarga | 1 | `https://bibliotecadigital.fgv.br/dspace;/bitstream/handle/10438/8526/MPEE-PDF-Full%20Book.pdf?sequen` |  |
| http_504 | descarga | 1 | `http://extranet.hgf.ce.gov.br/jspui/bitstream/123456789/366/1/2018_TCR_GinecologiaeObstetr%C3%ADcia_` | tipo=text/html |
| http_410 | descarga | 1 | `https://repositorio.uniceub.br/jspui/bitstream/235/11777/1/21308016.pdf` | tipo=text/html |
| ClientPayloadError | descarga | 1 | `https://burjcdigital.urjc.es/bitstreams/609ccf16-dc2f-0c23-e053-6f19a8c0ba23/download` |  |
| http_401 | descarga | 1 | `https://colecaoicnova.fcsh.unl.pt/index.php/icnova/article/download/114/123` | tipo=text/html |
| http_202 | descarga | 1 | `http://ageconsearch.umn.edu/bitstream/129185/2/(19)%20Baptista4.pdf` | tipo=text/html; charset=UTF-8 |
| http_400 | descarga | 1 | `https://idus.us.es/bitstream/handle/11441/68788/14.%20Traducci%F3n.pdf;jsessionid=0E4CB222F05F77275D` | tipo=text/html; charset=utf-8 |

## Subida a S3

El motor guarda cada archivo mientras lo baja (los de menos de 8 MB en el spool en RAM, los demás en disco) y, al terminar, lo deja para un proceso subidor aparte (boto3, en su propio núcleo) que lo sube a S3 (real o, en los ensayos, un S3 falso local: la columna "Dónde sube"). Si lo pendiente pasa 256 MB en RAM o 4.096 MB en disco, el motor deja de repartir tareas nuevas ("frenado"). **Lo que importa: frenado en 0 y lo pendiente estable** quieren decir que la descarga nunca esperó a la subida.

| Esc. | Dónde sube | Lugares | Tope de red | MB/s bajados | MB/s subidos | CPU motor | CPU motor sin subida | RSS motor (MB) | CPU subidor (media / máx) | RSS subidor (MB) | Pendiente máx RAM / disco (MB) | Frenado (s) | Escritura a disco (MB/s) | Objetos (multipart) | Errores de subida | Pendiente al final (MB) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| r25-host | proceso aparte (16), S3 real | 25 | sin tope | 23,1 | 23,9 | 29,7 | — | 261 | 13,7 / 157,0 | 127 | 30 / 536 | 0,0 | 1,3 | 4954 (0) | 0 | 0 |
| r50-host | proceso aparte (16), S3 real | 50 | sin tope | 26,7 | 26,4 | 38,2 | — | 331 | 18,0 / 120,4 | 127 | 36 / 635 | 0,0 | 1,3 | 11444 (0) | 0 | 0 |
| r75-host | proceso aparte (16), S3 real | 75 | sin tope | 31,8 | 31,5 | 45,3 | — | 407 | 21,8 / 128,4 | 127 | 46 / 548 | 0,0 | 3,1 | 14163 (0) | 0 | 0 |
| r120-host | proceso aparte (16), S3 real | 120 | sin tope | 31,2 | 31,3 | 46,1 | — | 389 | 21,9 / 148,2 | 129 | 60 / 968 | 0,0 | 2,6 | 15186 (0) | 0 | 0 |

- **Dónde sube:** proceso aparte (en su propio núcleo), un pool de hilos con s3transfer dentro del proceso del motor, o un pool de corrutinas con aiobotocore dentro del bucle del motor. Entre paréntesis, las subidas a la vez. El spool es el mismo en los tres: la única diferencia es dónde corre la subida.
- **CPU motor contra la referencia sin subida:** en proceso aparte, lo que le agrega al motor escribir el spool; con hilos o corrutinas, además todo el costo de subir, que comparte el núcleo (y el GIL) con la descarga. Si la CPU llega al 100 %, bajan los MB/s bajados y sube el retraso del bucle.
- **CPU del subidor:** solo se ve aparte en el proceso aparte. Con el SDK CRT de AWS sería menos.
- **Escritura a disco:** de toda la máquina; es lo que tiene que sostener el volumen EBS del spool (gp3 da 125 MB/s de base) y el tope de EBS de la instancia.
- En corridas cortas pasan sobre todo los archivos chicos: los grandes no llegan a terminar de bajar, así que el spool en disco aparece menos de lo que se usaría en la descarga real.
