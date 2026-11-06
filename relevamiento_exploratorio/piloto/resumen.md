# Resumen del relevamiento exploratorio

11 repositorios, 122 pedidos.

## 1. Plataforma y versión

| repo | plataforma | versión | fuente | todas las detecciones |
| --- | --- | --- | --- | --- |
| ar-sedici | dspace | 5.10-SNAPSHOT | generator | generator=dspace 5.10-SNAPSHOT; html=dspace; oai_identify=eprints |
| cl-uchile | dspace | 6.3 | rest56 | generator=dspace 6.3; html=dspace; rest56=dspace 6.3 |
| ar-rdu-unc | dspace | 7.6.7 | rest7 | generator=dspace 7.6.7; html=dspace; rest7=dspace 7.6.7 |
| pe-pucp |  |  |  |  |
| uy-colibri | dspace | 9.1 | rest7 | generator=dspace 9.1; html=dspace; rest7=dspace 9.1 |
| ar-conicet | dspace | 10.1 | rest7 | rest7=dspace 10.1 |
| ar-sadio-ejs | ojs | 3.4.0.8 | generator | generator=ojs 3.4.0.8; html=ojs; oai_identify=ojs |
| cr-rbt | ojs | 3.2.1.4 | generator | generator=ojs 3.2.1.4; html=ojs; oai_identify=ojs |
| ar-libros-unlp | omp | 1.1.1.1 | generator | generator=omp 1.1.1.1; html=ojs; oai_identify=omp |
| mx-ru-economicas | eprints | 3.3.16 | generator | generator=eprints 3.3.16; html=eprints; oai_identify=eprints |
| br-ufsc |  |  |  |  |

Plataforma detectada en 9 de 11; versión en 9.

| fuente | repositorios |
| --- | --- |
| generator | 5 |
| rest7 | 3 |
| sin detección | 2 |
| rest56 | 1 |

## 2. Métodos por plataforma y versión

Cada celda: ítems con resultado ok sobre ítems donde el método aplicaba, y mediana de pedidos de los ok.

| plataforma | directo | pagina | doi_handle | oai_xoai | oai_ore | oai_mets | rest56 | rest7 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dspace 5 | — | 0/3 | — | — | — | — | — | — |
| dspace 6 | 0/2 (2 sin probar) | 1/3 · 1 ped. (2 sin probar) | — | 1/3 · 1 ped. (2 sin probar) | 1/3 · 1 ped. (2 sin probar) | 1/3 · 1 ped. (2 sin probar) | 1/1 · 1 ped. | — |
| dspace 7 | — | — | 3/3 · 3 ped. | 3/3 · 1 ped. | 3/3 · 1 ped. | 3/3 · 1 ped. | — | 3/3 · 1 ped. |
| eprints 3 | 3/3 · 2 ped. | — | — | — | — | 3/3 · 1 ped. | — | — |
| ojs 3 | — | 6/6 · 1 ped. | — | — | — | — | — | — |

## 3. Archivos por ítem

| repo | ítem | archivos de contenido | verificados ok | tipos | bundles vistos |
| --- | --- | --- | --- | --- | --- |
| ar-rdu-unc | 0 | 2 | 1 | pdf | LICENSE×2 ORIGINAL×4 THUMBNAIL×2 |
| ar-rdu-unc | 1 | 2 | 1 | pdf | CC-LICENSE×2 LICENSE×2 ORIGINAL×4 TEXT×3 THUMBNAIL×2 |
| ar-rdu-unc | 2 | 2 | 1 | pdf | LICENSE×2 ORIGINAL×4 TEXT×3 THUMBNAIL×2 |
| ar-sadio-ejs | 0 | 1 | 1 | pdf |  |
| ar-sadio-ejs | 1 | 1 | 1 | pdf |  |
| ar-sadio-ejs | 2 | 1 | 1 | pdf |  |
| ar-sedici | 0 | 0 | 0 |  |  |
| ar-sedici | 1 | 0 | 0 |  |  |
| ar-sedici | 2 | 0 | 0 |  |  |
| cl-uchile | 0 | 2 | 0 |  | CC-LICENSE×2 LICENSE×2 ORIGINAL×4 |
| cl-uchile | 1 | 0 | 0 |  |  |
| cl-uchile | 2 | 0 | 0 |  |  |
| cr-rbt | 0 | 1 | 1 | pdf |  |
| cr-rbt | 1 | 1 | 1 | pdf |  |
| cr-rbt | 2 | 1 | 1 | pdf |  |
| mx-ru-economicas | 0 | 2 | 2 | pdf | reference×1 |
| mx-ru-economicas | 1 | 3 | 3 | pdf | reference×2 |
| mx-ru-economicas | 2 | 3 | 3 | jpeg pdf | reference×2 |

DSpace: en 4 de 4 ítems con bundles conocidos, todo el contenido está en ORIGINAL.

## 4. Tipos de URL

| tipo | ítems | % |
| --- | --- | --- |
| pagina | 12 | 67 |
| handle | 3 | 17 |
| archivo | 3 | 17 |

## 5. Anti-bot, 403, 429 y TLS

| repo | anti-bot | 403 | 429 | TLS inválido | abandono |
| --- | --- | --- | --- | --- | --- |
| cl-uchile | anubis (repositorio.uchile.cl) | 0 | 0 |  | repositorio.uchile.cl: anti-bot: anubis |
| pe-pucp | anubis (repositorio.pucp.edu.pe) | 0 | 0 |  | repositorio.pucp.edu.pe: anti-bot: anubis |
| uy-colibri |  | 2 | 0 |  |  |

Con anti-bot: 2 · con 403: 1 · con 429: 0 · con TLS inválido: 0 (de 11).

## 6. Latencias (ms hasta los encabezados)

| repo | mediana | p90 |
| --- | --- | --- |
| ar-sedici | 266 | 845 |
| cl-uchile | 226 | 566 |
| ar-rdu-unc | 212 | 877 |
| pe-pucp | 634 | 634 |
| uy-colibri | 198 | 208 |
| ar-conicet | 206 | 240 |
| ar-sadio-ejs | 287 | 445 |
| cr-rbt | 919 | 2714 |
| ar-libros-unlp | 125 | 926 |
| mx-ru-economicas | 723 | 1045 |
| br-ufsc | 218 | 262 |

Global: mediana 278 ms, p90 1038 ms.

## Cortesía

verificar-cortesia: **OK**

122 pedidos a 13 hosts en 1 corrida(s), 293.9 s. Hueco mínimo entre pedidos al mismo host: 5.0 s. Máximo en vuelo: 3.

Desvíos de la spec: tope total de 120 s por pedido; eventos.jsonl como registro adicional; los bundles THUMBNAIL, TEXT, LICENSE y CC-LICENSE no se verifican.
