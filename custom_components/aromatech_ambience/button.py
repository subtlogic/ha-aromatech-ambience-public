"""Remove the superseded one-shot button; Always on is now a native switch."""
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    from homeassistant.helpers import entity_registry as er

    coordinator = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, f"{coordinator.address}_set_always_on")
    old = registry.async_get(entity_id) if entity_id else None
    if old is not None and old.config_entry_id == entry.entry_id:
        registry.async_remove(entity_id)
    # Keep this platform for registry cleanup only; no duplicate control is added.
