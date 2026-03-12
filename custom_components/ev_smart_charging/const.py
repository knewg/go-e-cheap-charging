DOMAIN = "ev_smart_charging"

# Config entry keys
CONF_CHARGER_SERIAL = "charger_serial"
CONF_CAR_SOC_ENTITY = "car_soc_entity"
CONF_CAR_MAX_SOC_ENTITY = "car_max_soc_entity"
CONF_CAR_DEVICE_ID = "car_device_id"
CONF_PHASE_L1_ENTITY = "phase_l1_entity"
CONF_PHASE_L2_ENTITY = "phase_l2_entity"
CONF_PHASE_L3_ENTITY = "phase_l3_entity"
CONF_BATTERY_CAPACITY = "battery_capacity_kwh"
CONF_EFFICIENCY = "charge_efficiency"
CONF_BREAKER_LIMIT = "breaker_limit_a"
CONF_CHARGER_PHASE = "charger_phase"
CONF_MIN_AMP = "min_amp"
CONF_MAX_AMP = "max_amp"

# MQTT topics (format with serial=)
MQTT_STATUS_TOPIC = "go-eCharger/{serial}/status"
MQTT_COMMAND_TOPIC = "go-eCharger/{serial}/cmd/set"

# Go-e car states
CAR_IDLE = 1
CAR_CHARGING = 2
CAR_CONNECTED = 3
CAR_COMPLETE = 4

# Weekday names, index 0 = Monday (matches datetime.weekday())
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Platforms
PLATFORMS = ["switch", "time", "number", "sensor"]

# Timing
AMP_ADJUST_INTERVAL_S = 30
PLUGIN_DELAY_S = 60

# Nordpool: tomorrow's prices appear after this local time
NORDPOOL_PRICES_AVAILABLE_HOUR = 13
NORDPOOL_PRICES_AVAILABLE_MINUTE = 30

# Defaults
DEFAULT_BATTERY_CAPACITY = 64.0
DEFAULT_EFFICIENCY = 0.90
DEFAULT_BREAKER_LIMIT = 20
DEFAULT_CHARGER_PHASE = 1
DEFAULT_MIN_AMP = 6
DEFAULT_MAX_AMP = 16
DEFAULT_TARGET_SOC = 80
DEFAULT_CHEAP_THRESHOLD = 0.0
