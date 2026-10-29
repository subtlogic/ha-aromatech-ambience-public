"""Config flow for the AromaTech Ambience.

Discovery matches on the local name, because the Ambience carries no
manufacturer data in its advertisement - only in the scan response, which
ESPHome Bluetooth proxies do not forward. Matching on manufacturer id would
therefore never fire through a proxy, which is how this device reaches Home
Assistant here.
"""
from __future__ import annotations

from typing import Any
import math

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    CONF_EXPERIMENTAL_SCHEDULE_WRITES,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    SCAN_INTERVAL_SECONDS,
)

NAME_PREFIX = "Ambience"


def _is_ambience(name: str | None) -> bool:
    return bool(name) and name.startswith(NAME_PREFIX)


class AmbienceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Adopt an Ambience, by discovery or by picking it from a list."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> AmbienceOptionsFlow:
        return AmbienceOptionsFlow()

    def __init__(self) -> None:
        self._discovered: bluetooth.BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _is_ambience(discovery_info.name):
            return self.async_abort(reason="not_supported")
        self._discovered = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovered is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered.name or "Ambience",
                data={CONF_ADDRESS: self._discovered.address},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._discovered.name or "",
                "address": self._discovered.address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Ambience", data={CONF_ADDRESS: address})

        current = self._async_current_ids()
        devices = {
            info.address: f"{info.name} ({info.address})"
            for info in bluetooth.async_discovered_service_info(self.hass)
            if info.address not in current and _is_ambience(info.name)
        }
        if not devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(devices)}),
        )


class AmbienceOptionsFlow(OptionsFlow):
    """Manage settings and inventory without reloading for inventory mutations."""

    MENU = ["settings", "add_scent", "edit_scent", "delete_scent", "add_bottle",
            "edit_bottle", "install_bottle", "store_bottle", "record_weight",
            "correct_weight", "refill_bottle", "archive_bottle", "delete_bottle",
            "acknowledge"]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=self.MENU)

    async def async_step_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                validated = self._settings_schema()(user_input)
                for key in ("refill_days", "low_oil_g", "depleted_oil_g"):
                    validated[key] = _quantity(validated[key])
                if validated["low_oil_g"] <= validated["depleted_oil_g"]:
                    errors["low_oil_g"] = "threshold_order"
                else:
                    return self.async_create_entry(data={**self.config_entry.options, **validated})
            except vol.Invalid:
                errors["base"] = "invalid_input"
        return self.async_show_form(step_id="settings", data_schema=self._settings_schema(), errors=errors)

    def _settings_schema(self):
        options = self.config_entry.options
        return vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL_SECONDS)):
                vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)),
            vol.Required(CONF_EXPERIMENTAL_SCHEDULE_WRITES, default=options.get(CONF_EXPERIMENTAL_SCHEDULE_WRITES, False)): bool,
            vol.Required("refill_notifications", default=options.get("refill_notifications", False)): bool,
            vol.Required("refill_days", default=options.get("refill_days", 7)): _number(),
            vol.Required("low_oil_g", default=options.get("low_oil_g", 5)): _number(),
            vol.Required("depleted_oil_g", default=options.get("depleted_oil_g", 0)): _number(),
        })

    def _coordinator(self):
        return self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)

    def _inventory_schema(self, action, inventory):
        fields = {}
        if action in ("edit_scent", "delete_scent", "add_bottle", "refill_bottle"):
            fields[vol.Required("scent_id")] = _choices(inventory.scents)
        if action not in ("add_scent", "edit_scent", "delete_scent", "add_bottle", "acknowledge"):
            bottles = inventory.bottles
            if action == "store_bottle":
                bottles = {key: value for key, value in bottles.items() if key == inventory.active_bottle_id}
            elif action == "install_bottle":
                bottles = {key: value for key, value in bottles.items() if not value.get("archived", False)}
            fields[vol.Required("bottle_id")] = _choices(bottles)
        if action in ("add_scent", "add_bottle"):
            fields[vol.Required("name")] = str
        elif action in ("edit_scent", "edit_bottle"):
            fields[vol.Optional("name")] = str
        if action in ("add_scent", "edit_scent"):
            for key in ("brand", "notes"):
                fields[vol.Optional(key)] = str
            for key in ("grams_per_spray", "spray_seconds"):
                fields[vol.Optional(key)] = _number()
        if action in ("add_bottle", "edit_bottle"):
            fields[vol.Optional("empty_g")] = _number()
            fields[vol.Optional("notes")] = str
        if action in ("add_bottle", "refill_bottle"):
            fields[vol.Optional("bottle_size", default="60")] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=[
                    {"value": "30", "label": "30 — approximate starting amount"},
                    {"value": "60", "label": "60 — approximate starting amount"},
                    {"value": "120", "label": "120 — approximate starting amount"},
                    {"value": "custom", "label": "Custom oil amount or measured gross weight"},
                ], mode=selector.SelectSelectorMode.DROPDOWN))
            fields[vol.Optional("oil_g")] = _number()
        if action in ("add_bottle", "refill_bottle"):
            fields[vol.Optional("gross_g")] = _number()
        if action in ("record_weight", "correct_weight"):
            fields[vol.Required("gross_g")] = _number()
        if action == "correct_weight":
            fields[vol.Required("reason")] = str
        if action == "archive_bottle":
            fields[vol.Required("archived", default=True)] = bool
        if action in ("delete_scent", "delete_bottle"):
            fields[vol.Required("confirm", default=False)] = bool
        return vol.Schema(fields)

    async def _inventory_step(self, action, user_input):
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        schema = self._inventory_schema(action, coordinator.inventory)
        errors = {}
        placeholders = {"error": ""}
        if user_input is not None:
            try:
                fields = schema(user_input)
                size = fields.pop("bottle_size", None)
                if size is not None and "oil_g" not in fields and "gross_g" not in fields:
                    if size == "custom":
                        raise vol.Invalid("Enter a custom oil amount or measured gross weight")
                    fields["oil_g"] = float(size)
                for key in ("oil_g", "gross_g", "empty_g", "grams_per_spray", "spray_seconds"):
                    if key in fields:
                        fields[key] = _quantity(fields[key])
                if action.startswith("delete_") and fields.get("confirm") is not True:
                    errors["confirm"] = "confirmation_required"
                elif "oil_g" in fields and "gross_g" in fields:
                    errors["gross_g"] = "one_weight"
                else:
                    if action == "acknowledge":
                        await coordinator.async_acknowledge_refill()
                    else:
                        await coordinator.async_inventory_action(action, **fields)
                    return await self.async_step_init()
            except vol.Invalid:
                errors["base"] = "invalid_input"
            except (ValueError, HomeAssistantError) as err:
                errors["base"] = "operation_failed"
                placeholders["error"] = str(err)
        return self.async_show_form(step_id=action, data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)

    async def async_step_add_scent(self, user_input=None):
        return await self._inventory_step("add_scent", user_input)

    async def async_step_edit_scent(self, user_input=None):
        return await self._inventory_step("edit_scent", user_input)

    async def async_step_delete_scent(self, user_input=None):
        return await self._inventory_step("delete_scent", user_input)

    async def async_step_add_bottle(self, user_input=None):
        return await self._inventory_step("add_bottle", user_input)

    async def async_step_edit_bottle(self, user_input=None):
        return await self._inventory_step("edit_bottle", user_input)

    async def async_step_install_bottle(self, user_input=None):
        return await self._inventory_step("install_bottle", user_input)

    async def async_step_store_bottle(self, user_input=None):
        return await self._inventory_step("store_bottle", user_input)

    async def async_step_record_weight(self, user_input=None):
        return await self._inventory_step("record_weight", user_input)

    async def async_step_correct_weight(self, user_input=None):
        return await self._inventory_step("correct_weight", user_input)

    async def async_step_refill_bottle(self, user_input=None):
        return await self._inventory_step("refill_bottle", user_input)

    async def async_step_archive_bottle(self, user_input=None):
        return await self._inventory_step("archive_bottle", user_input)

    async def async_step_delete_bottle(self, user_input=None):
        return await self._inventory_step("delete_bottle", user_input)

    async def async_step_acknowledge(self, user_input=None):
        return await self._inventory_step("acknowledge", user_input)


def _quantity(value):
    if isinstance(value, bool):
        raise vol.Invalid("Enter a finite nonnegative quantity")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as err:
        raise vol.Invalid("Enter a finite nonnegative quantity") from err
    if not math.isfinite(number) or not 0 <= number <= 10000:
        raise vol.Invalid("Enter a finite quantity from 0 to 10000")
    return number


def _choices(records):
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=[{"value": identifier, "label": f"{record['name']} ({identifier[-8:]})"}
                 for identifier, record in records.items()],
        mode=selector.SelectSelectorMode.DROPDOWN,
    ))


def _number():
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=0, max=10000, step="any", mode=selector.NumberSelectorMode.BOX,
    ))
