"""Constants for the AromaTech Ambience integration."""

DOMAIN = "aromatech_ambience"

CONF_ADDRESS = "address"

# Reserved compatibility constant; ordinary commands do not use authentication.
DEFAULT_PASSWORD = "8888"

# How long to wait for the device to answer a command on the notify
# characteristic before giving up on reading state back. The observed
# round-trip was 70-380 ms; this is generous.
REPLY_TIMEOUT = 5.0

# The device is reached through whichever ESPHome Bluetooth proxy can hear it.
# Connections are made per command and dropped afterwards rather than held,
# because an ESP32 proxy has only three connection slots and this device is
# not worth camping one.
DISCONNECT_DELAY = 20.0

SCAN_INTERVAL_SECONDS = 300

# Options-flow key for the heartbeat interval, in seconds.
CONF_SCAN_INTERVAL = "scan_interval"
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 3600

CONF_EXPERIMENTAL_SCHEDULE_WRITES = "experimental_schedule_writes"
