# ElbaMeteo — ECMWF IFS a 9 km, sei giorni

**La configurazione corrente usa ECMWF IFS 9 km tramite Open-Meteo Single Runs,
con quattro run al giorno e un orizzonte di 144 ore.**
Le istruzioni aggiornate sono in [README-9km.md](README-9km.md).

Il seguito documenta il precedente canale GRIB a 0,25°, ancora utilizzato
soltanto per l'ensemble. Non descrive il download del vento principale attuale.

---

# Canale GRIB ECMWF a 0,25° (documentazione precedente)

Programma Python da riga di comando per acquisire ciclicamente il vento a 10 m,
conservare le singole run in SQLite e confrontarle alla stessa ora di validità.
La configurazione iniziale contiene **14 punti di griglia (P05–P18)**, scelti per
coprire la variabilità meteorologica dell'intera Isola d'Elba, non
località definitive. La raccolta continua si avvia esplicitamente con `watch`.

## Dati e risoluzione

Verifica del catalogo: 3 settembre 2026, ciclo IFS 50r1.

- Modello fisico **ECMWF IFS**, prodotto di controllo `oper/fc` e 50 membri
  perturbati `enfo/pf`. Le statistiche ensemble usano i 50 perturbati; il
  controllo è archiviato e presentato separatamente.
- Griglia pubblica gratuita **0,25° × 0,25°**, circa 20 × 28 km all'Elba.
  Questa è la risoluzione del dataset scaricato, distinta dalla griglia nativa
  del modello. I prodotti a risoluzione superiore hanno altre modalità di accesso.
- Run 00 e 12 UTC: da +0 a +144 ore ogni 3 ore, poi fino a +360 ore ogni 6 ore
  (85 scadenze). Run 06 e 18 UTC: da +0 a +144 ore ogni 3 ore (49 scadenze).
- `10u`, `10v`: componenti del vento, modulo e direzione di provenienza
  (0° nord, 90° est). Il modulo rappresenta il vento previsto al tempo valido,
  **non una media temporale sulle 3/6 ore**. Tutti i valori sono a 10 metri.
- `10fg`: massima raffica nell'intervallo indicato dal GRIB. A +0 la raffica
  è intenzionalmente assente. Si conservano gli estremi esatti dell'intervallo.
- Velocità esposte in km/h, anche nei confronti e nelle statistiche ensemble.
  Archivio grezzo e calcoli interni in m/s; conversione automatica in uscita.
  Date nel database e CSV sempre in UTC.

Il campionamento usa il punto di griglia più vicino, senza interpolazione.
Si registrano coordinate richieste, coordinate effettive e distanza in km.
Due località possono avere gli stessi valori se ricadono sullo stesso punto.
Questa griglia non risolve differenze fra spiagge vicine, promontori o effetti
locali dell'orografia. I tre punti provvisori sono a latitudine 42,75° e
longitudini 10°, 10,25°, 10,5°.

Fonti ufficiali:

- [Catalogo ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
- [Client Python ECMWF](https://github.com/ecmwf/ecmwf-opendata)
- [Portale dati](https://data.ecmwf.int/forecasts/)

Dati ECMWF, licenza **CC BY 4.0**: mantenere l'attribuzione ECMWF quando si
condividono dati o elaborazioni. Le statistiche di confidenza sono elaborazioni
di questo programma, non indici ufficiali ECMWF.

## Installazione e avvio su Windows

Serve Python 3.11 o successivo. Da PowerShell, nella cartella del progetto:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe elbameteo.py watch
```

In questa cartella è già stato preparato `.venv` con le dipendenze.
Non occorrono chiavi API. Ctrl+C interrompe la raccolta conservando i batch
già salvati. Per funzionare senza interruzioni il computer deve rimanere acceso
e il processo attivo. Il programma non installa automaticamente servizi o
attività pianificate. Si può eseguire `fetch` ogni ora tramite Utilità di
pianificazione, impostando questa cartella come directory di lavoro ed
evitando esecuzioni sovrapposte.

Comandi:

```powershell
# Recupera le run recenti e i batch mancanti, poi termina
.\.venv\Scripts\python.exe elbameteo.py fetch

# Intero orizzonte di una run specifica ancora online
.\.venv\Scripts\python.exe elbameteo.py fetch --run 2026-09-03T00:00Z

# Test contenuto: solo controllo, una scadenza (archivio PARZIALE)
.\.venv\Scripts\python.exe elbameteo.py fetch --run 2026-09-03T00:00Z --steps 3 --product fc

.\.venv\Scripts\python.exe elbameteo.py status
.\.venv\Scripts\python.exe elbameteo.py export --output previsioni.csv

.\.venv\Scripts\python.exe elbameteo.py compare --older 2026-09-02T00:00Z --newer 2026-09-03T00:00Z --location griglia_centro --output confronto.csv

.\.venv\Scripts\python.exe -m unittest -v
```

Le date sono esempi: il portale mantiene solo un archivio mobile di pochi
giorni. Le run già eliminate dal portale non si possono recuperare con questo
programma. Per costruire uno storico bisogna mantenerlo in esecuzione.

## Visualizzazione dell'ultima previsione

Dopo l'acquisizione di almeno una run completa:

```powershell
.\.venv\Scripts\python.exe forecast_visualization.py
```

Il comando produce `output/ultima_previsione.png`, riferito alla prima ora non
trascorsa, e `output/ultima_previsione.html`. La pagina interattiva sovrappone
alle aree una freccia orientata verso il moto del vento e la velocità in km/h;
il cursore e il comando di riproduzione mostrano l'evoluzione ora per ora. La
selezione di P05–P18 aggiorna i grafici di velocità, raffica e direzione del
nodo, con asse temporale suddiviso per giorno e ore italiane (`Europe/Rome`,
ora solare o legale applicata automaticamente).
Per ottenere il PNG di una scadenza specifica usare, per esempio, `--step 24`.

### Pubblicazione automatica con GitHub Pages

Il workflow `.github/workflows/publish-forecast.yml` aggiorna la previsione e
pubblica la pagina ogni tre ore (al minuto 17, orario UTC). È possibile avviarlo
anche manualmente dalla scheda **Actions** del repository.

Dopo avere caricato il progetto su un repository GitHub:

1. aprire **Settings → Pages**;
2. in **Build and deployment**, impostare **Source** su **GitHub Actions**;
3. aprire **Actions**, scegliere **Aggiorna e pubblica la previsione** e usare
   **Run workflow** per la prima pubblicazione.

La pagina sarà disponibile all'indirizzo mostrato dal job `deploy`, normalmente
`https://NOME-UTENTE.github.io/NOME-REPOSITORY/`. Il database SQLite usato dal
runner è temporaneo: ogni esecuzione acquisisce direttamente l'ultima run ECMWF
utile, quindi il sito non dipende dai file presenti sul computer locale.

La curva verde è un **indice di prevedibilità ensemble** da 0 a 100: combina
l'accordo fra le direzioni previste dai 51 membri ECMWF e la dispersione delle
velocità. È un indicatore diagnostico comparativo, non una probabilità calibrata
che la previsione si verifichi. I valori sono archiviati nella tabella SQLite
`predictability9` insieme alla run utilizzata.

Il profilo dell'Elba è suddiviso in brevi tratti: ogni tratto diventa rosso se
il vento del nodo ECMWF più vicino ha avuto una componente diretta verso
l'interno locale della costa in almeno una delle ultime sei ore disponibili;
in caso contrario resta verde.

## Configurazione

Modificare `config.json` prima di avviare la raccolta:

- `locations`: elenco di `id`, `lat`, `lon`. I nomi dei punti possono essere
  sostituiti con paesi, spiagge o coordinate da concordare.
- `poll_minutes`: pausa dopo ogni ciclo, inizialmente 60 minuti.
- `lookback_hours`: finestra di recupero, inizialmente 72 ore; massimo 96.
- `publication_delay_hours`: ritardo prudenziale iniziale, 8 ore. I prodotti
  non ancora disponibili vengono ritentati nei cicli successivi.
- `ensemble_members`: 50 per l'ensemble perturbato completo; 0 disabilita
  l'ensemble e quindi la confidenza. Un valore ridotto è utile per collaudi,
  ma non equivale all'ensemble completo.
- `source`: `ecmwf`, `aws` o `google`, repliche supportate dal client ufficiale.
- `database`: percorso relativo al file di configurazione.

Il portale distribuisce campi globali GRIB: si scaricano solo le variabili
richieste, ma **non si possono scaricare soltanto le celle dell'Elba** con
questo client. Le 50 perturbazioni su tutte le scadenze possono comportare
**decine di GB di traffico al giorno**, e il primo recupero di 72 ore è più
oneroso. L'ordine di grandezza dipende dalla compressione. L'archivio SQLite
finale è molto più piccolo: dopo l'estrazione i GRIB temporanei sono eliminati.
Per un primo collaudo usare `--steps 3 --product fc`.

Ogni batch (run/prodotto/scadenza) viene validato e salvato in una transazione.
Un errore non produce un batch dichiarato completo. Ai riavvii si saltano i
batch già acquisiti. Un errore interrompe quel prodotto per quella run e il
ciclo seguente riprova; si conservano quindi anche stati parziali, visibili
con `status`. Gli HTTP 429/503 vengono gestiti dal client con tentativi limitati.
La finestra di recupero è finita: un fermo di molti giorni può creare lacune
non recuperabili. `watch` non è stato avviato come servizio in background.

## Archivio e confronti

`forecasts` conserva componenti u/v originali, modulo, direzione, raffica,
intervalli e singoli membri. `batches` conserva fonte, ora di acquisizione e
SHA-256 dei GRIB scaricati. `configurations` conserva modello, griglia, metodo
e punti scelti. Un cambio delle località o del numero di membri crea una nuova
identità di configurazione e non sovrascrive i dati precedenti. I comandi
operano sulla configurazione attuale: conservare i vecchi JSON per consultare
i relativi dati. Aggiungere in futuro una località non ricostruisce i suoi dati
storici perché i GRIB globali non sono conservati.

`export` produce CSV con controllo e statistiche dei membri disponibili.
`compare` confronta il controllo di due run, unendo per **stessa validità UTC**
e stessa località, senza interpolazioni temporali. Le differenze sono
nuova meno vecchia. Le differenze angolari sono nell'intervallo [-180°, 180°).
Per raffiche con intervalli diversi la differenza rimane vuota e
`gust_intervals_match` è falso. Si confrontano solo le scadenze comuni presenti:
controllare `status` per sapere se le run sono complete.

Il confronto misura revisioni/stabilità delle previsioni. La run più nuova
non è un'osservazione: per verificare l'accuratezza andranno aggiunti dati
osservati con altezza, esposizione e intervalli compatibili.

## Confidenza/prevedibilità

L'esportazione riporta media, deviazione standard, percentili P10/P50/P90
del modulo e percentili delle raffiche. P10–P90 è la dispersione empirica dei
membri, non un intervallo di confidenza calibrato delle osservazioni future.

`direction_resultant` è la concentrazione circolare R delle direzioni (0–1):
vicino a 1 indica direzioni concordi. I membri sotto 0,5 m/s non hanno direzione
significativa; sono esclusi da R e conteggiati separatamente.

L'etichetta sperimentale `ensemble_spread_v1` riguarda **il vento**, non
l'accuratezza delle raffiche:

- `alta`: deviazione standard / max(media, 2 m/s) ≤ 0,25 e R ≥ 0,85;
- `media`: rapporto ≤ 0,50 e R ≥ 0,60;
- `bassa`: gli altri casi, inclusa direzione scarsamente definita;
- `non_disponibile`: meno di 20 membri o ensemble assente.

Per alta/media serve inoltre una direzione significativa in almeno l'80% dei
membri. Soglie euristiche dichiarate, da calibrare in seguito con osservazioni.
Anche un ensemble concorde può avere un errore comune sul vento locale.

## Verifiche

I quattro test automatici verificano direzione e discontinuità 359°/1°, griglia GRIB e metadati,
scadenze, completezza dei campi, idempotenza SQLite, dispersione ensemble e
confronto di raffiche con finestre differenti.

Collaudo online del 3 settembre 2026: acquisiti e decodificati realmente il
controllo della run 00 UTC alle scadenze +0, +3, +150 e +360 ore, e due membri
perturbati a +3 ore. Il database principale contiene questi quattro batch di
controllo; il collaudo ensemble ridotto è separato in `data/smoke.sqlite`.
La replica Google ha risposto correttamente; ECMWF e AWS hanno restituito
anche errori temporanei 429/503. Non è stata scaricata una run ensemble
completa e non è stato eseguito un collaudo continuativo di più giorni.
