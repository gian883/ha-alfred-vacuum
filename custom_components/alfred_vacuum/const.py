"""Costanti per l'integrazione Alfred Vacuum (Dreame D10 Plus)."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "alfred_vacuum"
MANUFACTURER: Final = "Dreame"
MODEL: Final = "dreame.vacuum.r2205"
MODEL_NAME: Final = "D10 Plus"

# Chiavi di configurazione
CONF_DID: Final = "did"
CONF_COUNTRY: Final = "country"
CONF_CLOUD: Final = "cloud"
CONF_USER_ID: Final = "user_id"
CONF_SERVICE_TOKEN: Final = "service_token"
CONF_SSECURITY: Final = "ssecurity"
CONF_REMEMBER_PASSWORD: Final = "remember_password"
CONF_ROOM_NAMES: Final = "room_names"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_MAP_INTERVAL: Final = "map_interval"

# Mappa dal vivo durante la pulizia
CONF_LIVE_MAP_INTERVAL: Final = "live_map_interval"
DEFAULT_LIVE_MAP_INTERVAL: Final = 30  # secondi (0 = spento)

# Idea 1: pulizia quando esci
CONF_AWAY_ENTITY: Final = "away_presence_entity"
CONF_AWAY_ROOMS: Final = "away_rooms"
CONF_AWAY_DELAY: Final = "away_delay"
CONF_AWAY_MIN_HOURS: Final = "away_min_hours"
CONF_AWAY_RETURN: Final = "away_return_home"
DEFAULT_AWAY_ENTITY: Final = "input_boolean.giggi_in_casa"
DEFAULT_AWAY_DELAY: Final = 10  # minuti
DEFAULT_AWAY_MIN_HOURS: Final = 12

# Idea 3: stanze trascurate
CONF_SMART_TIME: Final = "smart_time"
CONF_SMART_DAYS: Final = "smart_days"
CONF_SMART_MAX: Final = "smart_max_rooms"
CONF_SMART_EXCLUDE: Final = "smart_exclude"
DEFAULT_SMART_TIME: Final = "11:00:00"
DEFAULT_SMART_DAYS: Final = 3
DEFAULT_SMART_MAX: Final = 2

MIN_BATTERY_AUTO: Final = 40  # sotto questa carica le automazioni non partono

# Eventi
EVENT_CLEANING_FINISHED: Final = "alfred_vacuum_cleaning_finished"
EVENT_ROOMS_CHANGED: Final = "alfred_vacuum_rooms_changed"

# Immagine dell'ultima pulizia (per Telegram)
MEDIA_SUBDIR: Final = "alfred_vacuum"
LAST_CLEAN_IMAGE: Final = "ultima_pulizia.png"

DEFAULT_COUNTRY: Final = "de"
DEFAULT_SCAN_INTERVAL: Final = 15  # secondi
DEFAULT_MAP_INTERVAL: Final = 60  # minuti tra due letture della mappa
COUNTRIES: Final = ["de", "cn", "us", "ru", "sg", "i2", "tw"]

# Servizi
SERVICE_CLEAN_ROOMS: Final = "clean_rooms"
SERVICE_REFRESH_MAP: Final = "refresh_map"
SERVICE_SEND_COMMAND: Final = "send_command"
SERVICE_CLEAN_OVERDUE: Final = "clean_overdue"

# --- Specifica MIoT del D10 Plus (urn:miot-spec-v2:device:vacuum:0000A006:dreame-r2205:2)
# Ogni proprietà è (siid, piid).
P_STATUS: Final = (2, 1)
P_FAULT: Final = (2, 2)
P_FAN: Final = (2, 3)
P_BATTERY: Final = (3, 1)
P_CHARGING: Final = (3, 2)
P_WORK_MODE: Final = (4, 1)
P_CLEANING_TIME: Final = (4, 2)
P_CLEANING_AREA: Final = (4, 3)
P_MOP: Final = (4, 5)
P_WATERBOX: Final = (4, 6)
P_TASK_STATUS: Final = (4, 7)
P_RESUME: Final = (4, 11)
P_CARPET: Final = (4, 12)
P_FAULTS: Final = (4, 18)
P_DND: Final = (5, 1)
P_DND_START: Final = (5, 2)
P_DND_END: Final = (5, 3)
P_MULTI_MAP: Final = (6, 7)
P_VOLUME: Final = (7, 1)
P_MAIN_BRUSH_TIME: Final = (9, 1)
P_MAIN_BRUSH_LIFE: Final = (9, 2)
P_SIDE_BRUSH_TIME: Final = (10, 1)
P_SIDE_BRUSH_LIFE: Final = (10, 2)
P_FILTER_LIFE: Final = (11, 1)
P_FILTER_TIME: Final = (11, 2)
P_TOTAL_TIME: Final = (12, 2)
P_TOTAL_COUNT: Final = (12, 3)
P_TOTAL_AREA: Final = (12, 4)
P_AUTO_EMPTY: Final = (15, 1)
P_AUTO_EMPTY_EVERY: Final = (15, 2)
P_DUST_STATUS: Final = (15, 5)

POLLED_PROPERTIES: Final = [
    P_STATUS, P_FAULT, P_FAN, P_BATTERY, P_CHARGING, P_WORK_MODE,
    P_CLEANING_TIME, P_CLEANING_AREA, P_MOP, P_WATERBOX, P_TASK_STATUS,
    P_RESUME, P_CARPET, P_FAULTS, P_DND, P_VOLUME,
    P_MAIN_BRUSH_TIME, P_MAIN_BRUSH_LIFE, P_SIDE_BRUSH_TIME, P_SIDE_BRUSH_LIFE,
    P_FILTER_LIFE, P_FILTER_TIME, P_TOTAL_TIME, P_TOTAL_COUNT, P_TOTAL_AREA,
    P_AUTO_EMPTY, P_AUTO_EMPTY_EVERY, P_DUST_STATUS,
]

# Azioni (siid, aiid)
A_START: Final = (2, 1)
A_PAUSE: Final = (2, 2)
A_DOCK: Final = (3, 1)
A_START_CUSTOM: Final = (4, 1)
A_STOP: Final = (4, 2)
A_AREA_CLEAN: Final = (4, 7)
A_MAP_REQUEST: Final = (6, 1)
A_LOCATE: Final = (7, 1)
A_RESET_MAIN_BRUSH: Final = (9, 1)
A_RESET_SIDE_BRUSH: Final = (10, 1)
A_RESET_FILTER: Final = (11, 1)
A_EMPTY_BIN: Final = (15, 1)

# Parametri dell'azione start-area-clean
PIID_WORK_MODE: Final = 1
PIID_EXTEND_DATA: Final = 10
WORK_MODE_ROOMS: Final = 18

# Stato (siid 2 piid 1)
STATUS_SWEEPING: Final = 1
STATUS_IDLE: Final = 2
STATUS_PAUSED: Final = 3
STATUS_PAUSED_2: Final = 4
STATUS_RETURNING: Final = 5
STATUS_CHARGING: Final = 6
STATUS_MOPPING: Final = 7
STATUS_MAPPING: Final = 11
STATUS_CLEANING: Final = 12
STATUS_CHARGED: Final = 13
STATUS_UPGRADING: Final = 14

STATUS_NAMES: Final = {
    1: "Aspirazione",
    2: "In attesa",
    3: "In pausa",
    4: "In pausa",
    5: "Rientro alla base",
    6: "In carica",
    7: "Lavaggio",
    11: "Creazione mappa",
    12: "Pulizia",
    13: "Carica completata",
    14: "Aggiornamento",
}

FAN_SPEEDS: Final = {0: "Silenziosa", 1: "Standard", 2: "Forte", 3: "Turbo"}
MOP_LEVELS: Final = {1: "Bassa", 2: "Media", 3: "Alta"}
CHARGING_STATES: Final = {1: "In carica", 2: "Non in carica", 5: "Rientro"}

# Tipi di stanza usati dall'app Dreame (nome predefinito quando non è stato
# dato un nome personalizzato). Da verificare sul robot: vedi README.
ROOM_TYPES: Final = {
    0: "Stanza",
    1: "Soggiorno",
    2: "Camera da letto",
    3: "Studio",
    4: "Cucina",
    5: "Sala da pranzo",
    6: "Bagno",
    7: "Balcone",
    8: "Corridoio",
    9: "Lavanderia",
    10: "Cabina armadio",
    11: "Sala riunioni",
    12: "Ufficio",
    13: "Palestra",
    14: "Area giochi",
    15: "Camera secondaria",
}
