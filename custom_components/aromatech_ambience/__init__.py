"""AromaTech Ambience — local BLE control.

The existing community integration for AromaTech diffusers
(`ttrushin/ha-aromatech-scent-diffuser`) does not work with this model: it
writes to characteristic `fff6`, which the Ambience does not implement. This is
a separate integration for the `EE01` protocol family rather than a fork,
because the two protocols share no service, characteristic or framing.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import CONF_EXPERIMENTAL_SCHEDULE_WRITES, CONF_SCAN_INTERVAL, DOMAIN, SCAN_INTERVAL_SECONDS
from .coordinator import AmbienceCoordinator
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = AmbienceCoordinator(
        hass,
        entry.data[CONF_ADDRESS],
        entry.options.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL_SECONDS),
        time_zone=hass.config.time_zone,
        experimental_schedule_writes=entry.options.get(CONF_EXPERIMENTAL_SCHEDULE_WRITES, False),
        options=dict(entry.options),
    )
    await coordinator.async_initialize_storage(entry.entry_id)
    await coordinator.async_initialize_schedule_mode(entry.entry_id)
    # Deliberately NOT async_config_entry_first_refresh. That raises
    # ConfigEntryNotReady when the first connect fails, which takes the whole
    # integration down - and this device is documented as advertising
    # intermittently, so a miss at boot is expected rather than exceptional.
    # On a cold start the ESPHome proxies are coming up at the same moment, so
    # the first attempt is the one most likely to miss.
    #
    # async_refresh does not raise. Setup succeeds, the entities exist, and
    # CoordinatorEntity reports them unavailable until the first successful
    # poll fills them in - honest about the state instead of pretending the
    # integration is not installed. Recovery costs one scan interval.
    await coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        coordinator.async_start_timers()
    except Exception:
        await coordinator.async_close()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply alert preferences live; only BLE settings require a reload."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return
    previous = coordinator.options
    defaults = {CONF_SCAN_INTERVAL: SCAN_INTERVAL_SECONDS,
                CONF_EXPERIMENTAL_SCHEDULE_WRITES: False}
    if any(previous.get(key, default) != entry.options.get(key, default)
           for key, default in defaults.items()):
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        await coordinator.async_apply_inventory_options(dict(entry.options))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"aromatech_ambience.{entry.entry_id}").async_remove()
    from .inventory_store import InventoryStore
    from homeassistant.components import persistent_notification

    await InventoryStore(hass, entry.entry_id).async_remove()
    await InventoryStore(hass, entry.entry_id, suffix="schedule_mode").async_remove()
    persistent_notification.async_dismiss(hass, f"aromatech_ambience_refill_{entry.entry_id}")
