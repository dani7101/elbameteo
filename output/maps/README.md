# Mappa di riferimento Elba / ECMWF

- `elba_aree_ecmwf_9km.png`: mappa PNG delle aree di assegnazione ai nodi ECMWF.

La griglia è la **O1280 nativa**, non una griglia quadrata di 9 km disegnata
arbitrariamente. Le latitudini sono calcolate da ecCodes con
`codes_get_gaussian_latitudes(1280)`; alla fila j, con j=0 al polo nord,
ci sono `20 + 4*j` nodi equispaziati in longitudine a partire da 0°.
I confini mostrano le aree geometriche di prossimità (Voronoi) associate
al nodo ECMWF più vicino.

All'Elba il passo lungo una fila è circa 10,895 km e la separazione
nord-sud fra file è circa 7,809 km (distanze geodetiche WGS84).
La proiezione locale azimutale equidistante preserva bene le distanze
su questa piccola estensione. Il nodo 42,7768003952 N / 10,2514792899 E
coincide con quello restituito dall'API ECMWF 9 km di Open-Meteo.

La costa Natural Earth è generalizzata: la mappa serve come riferimento
alla scala del modello e non come carta di navigazione.
I centri abitati sono riferimenti geografici e non punti meteo selezionati.

Fonti:

- [ECMWF Atlas: definizione della griglia](https://sites.ecmwf.int/docs/atlas/design/grid/)
- [ECMWF: risoluzione dei punti di griglia](https://confluence.ecmwf.int/spaces/FUG/pages/673550363/Section%2B2A.1.1.1%2BGrid%2Bpoint%2Bresolution)
- [Costa Natural Earth, pubblico dominio](https://www.naturalearthdata.com/downloads/10m-physical-vectors/10m-land/)
- [Località GeoNames tramite Open-Meteo](https://open-meteo.com/en/docs/geocoding-api)

La mappa è prodotta deterministicamente da `create_grid_map.py` mediante
Matplotlib, pyproj ed ecCodes. Dipendenze aggiuntive: `requirements-map.txt`.
I dati sorgente scaricati sono conservati in `data/cartography/`.
