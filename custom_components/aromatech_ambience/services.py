"""Domain actions for indexed schedules and observed bottle checkpoints."""
from __future__ import annotations

import math
import re

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN


def clock_time(value):
    """Require an unambiguous minute-resolution local time."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise vol.Invalid("Use local time HH:MM, 00:00–23:59")
    return value


def integer(value):
    """Do not silently truncate slot, mask or intensity inputs."""
    if type(value) is not int:
        raise vol.Invalid("Expected an integer")
    return value


def grams(value):
    if isinstance(value, bool):
        raise vol.Invalid("Weight must be a finite number of grams")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as err:
        raise vol.Invalid("Weight must be a finite number of grams") from err
    if not math.isfinite(result) or not 0 <= result <= 10000:
        raise vol.Invalid("Weight must be 0–10000 grams")
    return result


def _coordinator(hass, entry_id):
    entry = hass.config_entries.async_get_entry(entry_id)
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    if entry is None or entry.domain != DOMAIN or coordinator is None:
        raise HomeAssistantError("Select a loaded AromaTech Ambience integration entry")
    return coordinator


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register once, independently of device setup success."""
    if hass.services.has_service(DOMAIN, "set_schedule"):
        return

    async def set_schedule(call: ServiceCall) -> None:
        data = call.data
        coordinator = _coordinator(hass, data["config_entry_id"])
        fields = {name: data[name] for name in ("enabled", "intensity") if name in data}
        if "days_mask" in data:
            fields["day_bits"] = data["days_mask"]
        for field, prefix in (("start", "start"), ("end", "end")):
            if field in data:
                hour, minute = map(int, data[field].split(":"))
                fields[f"{prefix}_hour"] = hour
                fields[f"{prefix}_minute"] = minute
        await coordinator.async_set_schedule(slot=data["slot"], allow_append=True, **fields)

    async def delete_schedule(call: ServiceCall) -> None:
        await _coordinator(hass, call.data["config_entry_id"]).async_delete_schedule(call.data["slot"])

    async def record_bottle_weight(call: ServiceCall) -> None:
        if call.data["gross_g"] <= 0 or call.data["gross_g"] < call.data["empty_g"]:
            raise HomeAssistantError("Gross weight must be positive and at least the empty bottle weight")
        await _coordinator(hass, call.data["config_entry_id"]).async_record_bottle_weight(
            call.data["gross_g"], call.data["empty_g"]
        )

    entry = {vol.Required("config_entry_id"): cv.string}
    slot = {**entry, vol.Required("slot"): vol.All(integer, vol.Range(min=1, max=5))}
    hass.services.async_register(DOMAIN, "set_schedule", set_schedule, schema=vol.Schema({
        **slot, vol.Optional("start"): clock_time, vol.Optional("end"): clock_time,
        vol.Optional("days_mask"): vol.All(integer, vol.Range(min=0, max=127)),
        vol.Optional("enabled"): cv.boolean,
        vol.Optional("intensity"): vol.All(integer, vol.Range(min=1, max=4)),
    }))
    hass.services.async_register(DOMAIN, "delete_schedule", delete_schedule, schema=vol.Schema(slot))
    hass.services.async_register(DOMAIN, "record_bottle_weight", record_bottle_weight, schema=vol.Schema({
        **entry, vol.Required("gross_g"): grams, vol.Required("empty_g"): grams,
    }))
    _setup_inventory_services(hass, entry)


def _setup_inventory_services(hass, entry):
    from .inventory import Inventory

    async def manage_inventory(call):
        fields = dict(call.data)
        coordinator = _coordinator(hass, fields.pop("config_entry_id"))
        action = fields.pop("action")
        await coordinator.async_inventory_action(action, **fields)

    async def list_inventory(call):
        coordinator = _coordinator(hass, call.data["config_entry_id"])
        if coordinator.inventory_manager is None:
            raise HomeAssistantError("Bottle inventory is not loaded")
        return coordinator.inventory_manager.export()

    async def acknowledge_refill(call):
        await _coordinator(hass, call.data["config_entry_id"]).async_acknowledge_refill()

    fields = {**entry, vol.Required("action"): vol.In(tuple(Inventory._FIELDS))}
    for name in ("name", "brand", "notes", "scent_id", "bottle_id", "reason"):
        fields[vol.Optional(name)] = cv.string
    for name in ("empty_g", "gross_g", "oil_g", "grams_per_spray", "spray_seconds"):
        fields[vol.Optional(name)] = grams
    for name in ("archived", "confirm"):
        fields[vol.Optional(name)] = cv.boolean
    hass.services.async_register(DOMAIN, "manage_inventory", manage_inventory, schema=vol.Schema(fields))
    hass.services.async_register(DOMAIN, "list_inventory", list_inventory, schema=vol.Schema(entry),
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "acknowledge_refill", acknowledge_refill, schema=vol.Schema(entry))
