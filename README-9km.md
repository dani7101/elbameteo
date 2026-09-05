# ECMWF IFS 9 km — configurazione corrente

Solo ECMWF. Nessun dato ICON e nessuna selezione automatica di altri modelli.
Il modello richiesto all'API è esplicitamente `ecmwf_ifs`, griglia nativa O1280.

**Tutte le velocità esposte sono in km/h**: vento, raffiche, statistiche
ensemble e differenze tra run. Le colonne CSV terminano in `_kmh`.
I dati grezzi nel database e i calcoli interni mantengono le unità SI;
la conversione in km/h avviene all'esportazione, anche per le run già archiviate.

- Run: **00, 06, 12, 18 UTC**, conservate separatamente.
- Orizzonte: **144 ore dalla partenza di ogni run**.
- Serie oraria: **145 istanti da +0 a +144**, includendo entrambi gli estremi.
- Fino a +90 ore: scadenze native orarie.
- Dopo +90: scadenze native ogni 3 ore; quelle intermedie sono interpolate
  da Open-Meteo. La colonna `temporal_origin` le distingue nell'esportazione.
  Questo flag deriva dal calendario documentato dal provider, non da un
  metadato di interpolazione presente su ciascun campione della risposta.
- Velocità e direzione sono riferite al vento a 10 m al tempo valido:
  una serie oraria non equivale a una media del vento nell'ora.

Fonti: [ECMWF su Open-Meteo](https://open-meteo.com/en/docs/ecmwf-api),
[Single Runs](https://open-meteo.com/en/docs/single-runs-api).
API gratuita per uso non commerciale secondo le
[condizioni Open-Meteo](https://open-meteo.com/en/pricing).
Attribuzione dati: ECMWF / Open-Meteo, CC BY 4.0.

## Avvio

L'ambiente `.venv` è già installato nella cartella. Se si ricrea l'ambiente,
usare Python 3.11+ e `pip install -r requirements.txt`.

```powershell
# Vento/raffiche 9 km, una run, tutte le 144 ore (download leggero)
.\.venv\Scripts\python.exe elbameteo.py fetch --run 2026-09-02T06:00Z --product fc

# Raccolta completa: vento 9 km e confidenza da ensemble ECMWF
.\.venv\Scripts\python.exe elbameteo.py fetch

# Raccolta ciclica, fino a Ctrl+C
.\.venv\Scripts\python.exe elbameteo.py watch

.\.venv\Scripts\python.exe elbameteo.py status
.\.venv\Scripts\python.exe elbameteo.py export --output previsioni.csv
.\.venv\Scripts\python.exe elbameteo.py compare --older 2026-09-02T00:00Z --newer 2026-09-02T06:00Z --location griglia_centro --output confronto.csv
.\.venv\Scripts\python.exe -m unittest -v
```

`--steps` non si usa con il provider 9 km: ogni richiesta salva l'intera
finestra di sei giorni per una località. La richiesta riceve sette giorni
di dati per includere l'estremo +144; il programma archivia solo +0..+144.
Ogni run/località viene confermata completa solo dopo la verifica di tutti
i valori; a +0 la raffica è assente. I dati ricevuti incompleti sono ritentati
al ciclo seguente. I batch già salvati non vengono scaricati nuovamente.

## Prevedibilità

Il vento principale è a **9 km**. Il canale ensemble già implementato rimane
**ECMWF IFS a 0,25°**, con 50 perturbazioni, entro +144 ore e a intervalli
di 3 ore. Non viene presentato come ensemble a 9 km. Le statistiche sono
abbinate alla stessa run, località richiesta e ora di validità; i punti di
griglia ensemble e controllo possono essere diversi.

La confidenza resta vuota nelle ore senza ensemble della stessa validità:
non viene interpolata. Nel CSV è indicata la risoluzione ensemble separatamente.
L'etichetta euristica descrive l'accordo sul vento, non una probabilità di
correttezza. Per formule e soglie vedere la documentazione del canale GRIB.

Il download dei punti a 9 km è leggero. **Il canale ensemble GRIB rimane
oneroso** perché scarica campi globali. `ensemble_members: 0` lo disattiva;
`--product fc` lo esclude dalla singola acquisizione. Nessun processo continuo
è stato avviato automaticamente.

## Archivio e raffiche

Le tabelle `hourly9` e `downloads9` conservano valori, coordinate restituite,
risposta JSON originale, richiesta, hash e ora di acquisizione. Il modello è
fissato nella richiesta e la run è esplicita: non si usa il forecast continuo
che potrebbe combinare aggiornamenti diversi. Il campionamento è `nearest`,
senza correzione di quota; le località attuali sono ancora esempi da concordare.

L'identità della configurazione è distinta dal precedente archivio a 0,25°,
che rimane intatto. I comandi mostrano i dati della configurazione corrente.

Open-Meteo elabora anche le raffiche e non espone nel JSON l'intervallo GRIB
esatto di ogni campione. Il programma conserva il valore fornito e segnala
questa limitazione, senza inventare estremi temporali. Il confronto mostra
le due raffiche, ma non calcola una differenza presumendo intervalli identici.
Le differenze di vento e direzione sono invece calcolate alle ore comuni,
con indicazione delle scadenze native/interpolate delle due run.
