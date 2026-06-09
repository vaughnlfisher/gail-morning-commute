"""Constants for gail_morning_commute."""

DOMAIN = "gail_morning_commute"

DARWIN_TOKEN = "001105bc-e005-48d1-a443-595d23aba5aa"

# CRS codes
LEG1_FROM = "TWY"   # Twyford
LEG1_TO   = "EAL"   # Ealing Broadway
LEG2_FROM = "EAL"   # Ealing Broadway
LEG2_TO   = "HMM"   # Hammersmith

# Interchange time at Ealing Broadway (Elizabeth line platform → District line)
EALING_INTERCHANGE_MINS = 5

NUM_TRAINS = 3
MAX_LEG2   = 3

SCAN_INTERVAL_PEAK    = 120
SCAN_INTERVAL_OFFPEAK = 300
SCAN_INTERVAL_NIGHT   = 900

HUXLEY_ROWS = 25

# Eastbound Elizabeth line termini from Twyford calling at Ealing Broadway
EASTBOUND_TERMINI = {
    "ealing broadway", "london paddington", "paddington",
    "whitechapel", "stratford", "shenfield",
    "abbey wood", "canary wharf", "liverpool street",
    "woolwich", "reading", "heathrow",
    "heathrow airport", "heathrow terminal",
}

# District/Piccadilly line termini from Ealing Broadway towards Hammersmith
HAMMERSMITH_TERMINI = {
    "hammersmith", "wimbledon", "earls court", "richmond",
    "edgware road", "putney bridge", "upminster",
    "barking", "tower hill", "monument",
}

# HSP — leg1 (TWY→EAL) is Elizabeth Line (TfL), no NR HSP.
# leg2 (EAL→HMM) is District line (TfL), no NR HSP.
# Proxy reliability from Vaughn's morning Elizabeth line sensor.
HSP_URL      = "https://hsp-prod.rockshore.net/api/v1/serviceMetrics"
HSP_USERNAME = "YOUR_NRE_USERNAME"
HSP_PASSWORD = "YOUR_NRE_PASSWORD"
LEG1_HISTORY_PROXY_ENTITY = "sensor.twyford_to_farringdon_historical_reliability"
LEG2_HISTORY_PROXY_ENTITY = "sensor.morning_commute_leg_2_historical_reliability"
