# Alfred Vacuum – Dreame D10 Plus per Home Assistant

Integrazione personalizzata per il robot **Dreame D10 Plus** (`dreame.vacuum.r2205`).
Comandi in **rete locale**, **nomi delle stanze** letti dalla mappa del robot, pulizia per stanza **anche per nome**.

> Progetto personale, supporta solo il D10 Plus. Non affiliato a Dreame o Xiaomi.

## Funzioni

| Tipo | Entità / servizio |
|---|---|
| Aspirapolvere | avvio, pausa, stop, ritorno alla base, localizza, potenza |
| Stanze | sensore **Stanze** (numero → nome), **Stanza attuale**, selettore **Stanza da pulire** + pulsante **Pulisci stanza selezionata** |
| Servizio `alfred_vacuum.clean_rooms` | `rooms: ["Cucina", "Bagno"]` oppure `[4, 2]`, passate, potenza, acqua |
| Sensori | batteria, stato, durata e area pulizia, errori, spazzole, filtro, totali |
| Impostazioni | potenza, acqua, svuotamento automatico (e ogni quante pulizie), tappeti, riprendi dopo ricarica, non disturbare, volume |
| Pulsanti | svuota contenitore, azzera spazzole e filtro, rileggi mappa |
| Diagnosi | servizio `alfred_vacuum.send_command` (legge/scrive proprietà, esegue azioni e restituisce la risposta) |

## Nomi stabili (anche dopo una nuova mappatura)

Nelle automazioni usa **sempre il nome** della stanza (`rooms: ["Cucina"]`), mai il numero.
L'integrazione traduce il nome nel numero **al momento del comando**, con la mappa attuale:
se rifai la mappatura e la cucina passa dal 4 al 2, le automazioni continuano a funzionare.

- Quando la mappa cambia arriva una **notifica** con la nuova tabella numero → stanza e l'evento `alfred_vacuum_rooms_changed`.
- Lo **storico** (ultima pulizia di ogni stanza) è legato al nome: una rimappatura non lo azzera.
- Dopo una rimappatura dai nell'app il nome o il tipo giusto a ogni stanza; con due stanze dello stesso tipo usa nomi personalizzati.

## Mappa

Entità `camera.<robot>_mappa`: stanze colorate con il nome, base di ricarica, robot con la direzione.
Durante la pulizia si aggiorna ogni 30 s (configurabile). Negli attributi ci sono `calibration_points`
e `rooms` per la card interattiva **Xiaomi Vacuum Map Card** (HACS → Frontend).

## Automazioni integrate (spente di default)

| Interruttore | Cosa fa | Opzioni |
|---|---|---|
| **Pulizia quando esci** | quando l'entità di presenza diventa "fuori casa" e resta tale per N minuti, pulisce le stanze scelte; al rientro manda il robot alla base | entità di presenza, stanze, attesa, intervallo minimo |
| **Stanze trascurate** | ogni giorno all'orario scelto pulisce le stanze non pulite da più di N giorni, dalla più trascurata | orario, giorni, massimo stanze, esclusioni |

Le automazioni non partono se il robot non è alla base o ha meno del 40% di batteria.
Servizio `alfred_vacuum.clean_overdue` per lanciarla a mano (`dry_run: true` = solo elenco).

## Fine pulizia

Evento `alfred_vacuum_cleaning_finished` con `stanze`, `area_m2`, `durata_min`, `avviata_da`, `immagine`
(PNG della mappa salvato in `/media/alfred_vacuum/ultima_pulizia.png`, utile per Telegram).
Sensore **Ultima pulizia** con gli stessi dati.

## Installazione (HACS)

1. HACS → menu ⋮ → **Repository personalizzati** → `https://github.com/gian883/ha-alfred-vacuum`, tipo **Integrazione**.
2. Installa **Alfred Vacuum** e riavvia Home Assistant.
3. Impostazioni → Dispositivi e servizi → **Aggiungi integrazione** → *Alfred Vacuum*.

## Collegamento

- **Account Xiaomi (consigliato)**: lo stesso dell'app Xiaomi Home. Serve per ottenere IP e token del robot e per scaricare la mappa (nomi delle stanze). I comandi vanno comunque in locale; il cloud si usa solo se il robot non risponde in rete.
  La **password non viene salvata**: si conservano solo i token di sessione. Quando scadono, Home Assistant chiede di accedere di nuovo.
  Se Xiaomi chiede la verifica in due passaggi, il codice arriva per email/SMS e si inserisce nella procedura.
- **Solo rete locale**: IP + token. Funziona tutto tranne i nomi delle stanze (si possono scrivere a mano nelle opzioni).

## Nomi delle stanze

Il robot carica la mappa sul cloud Xiaomi; l'integrazione la scarica ogni ora (configurabile) e quando il robot torna alla base, e legge:
- il **nome personalizzato** dato nell'app, se c'è;
- altrimenti il **tipo di stanza** (Cucina, Bagno, …).

Nelle **opzioni** puoi forzare i nomi: `1=Soggiorno, 2=Bagno, 4=Cucina`.

## Esempi

```yaml
action: alfred_vacuum.clean_rooms
data:
  rooms: ["Cucina"]
  fan_speed: Forte
```

```yaml
# pulizia della stanza scelta in dashboard
action: button.press
target:
  entity_id: button.alfred_pulisci_stanza_selezionata
```

## Come funziona (per chi vuole modificarla)

| File | Ruolo |
|---|---|
| `miio.py` | protocollo locale miio (UDP 54321, AES-128-CBC) |
| `cloud.py` | login Xiaomi, verifica in due passaggi, API firmate, download mappa |
| `device.py` | comandi del D10 Plus, scelta locale/cloud, richiesta mappa |
| `map_decoder.py` | lettura della mappa: stanze, nomi, posizione del robot |
| `coordinator.py` | aggiornamento periodico, stanze, ricerca per nome |
| `const.py` | proprietà e azioni MIoT del D10 Plus |

Fonti pubbliche usate: specifica MIoT `urn:miot-spec-v2:device:vacuum:0000A006:dreame-r2205:2` (miot-spec.org) e la documentazione pubblica dei protocolli miio e cloud Xiaomi.

## Licenza

MIT – © 2026 gian883
