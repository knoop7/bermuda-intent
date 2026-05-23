"""Bermuda intent handlers.

注册 HA 意图，让语音助手和外部 AI 可以实时查询和控制 Bermuda 蓝牙定位。
所有配置修改热加载，无需重启。
"""

from __future__ import annotations

import logging
import re
from time import monotonic

import voluptuous as vol
from homeassistant.components.bluetooth import (
    async_address_present,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_last_service_info,
    async_scanner_count,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from .const import (
    CONF_AREA_ENTITIES,
    CONF_AREA_ENTITY_DISTANCE,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_ERR_NOT_LOADED = (
    "Bermuda integration is not loaded. "
    "Possible causes: (1) integration not installed, (2) config entry disabled, "
    "(3) setup failed — check HA logs for 'bermuda' errors."
)
_ERR_NO_COORDINATOR = (
    "Bermuda coordinator unavailable. The integration may still be initializing. "
    "Wait a few seconds and retry, or check logs for errors."
)
_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_IBEACON_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_\d+_\d+$", re.IGNORECASE)
_MAX_CODE_LEN = 4096

INTENT_GET_CONFIG = "BermudaGetConfig"
INTENT_SET_CONFIG = "BermudaSetConfig"
INTENT_EXECUTE = "BermudaExecute"
INTENT_MANAGE_DEVICE = "BermudaManageDevice"
INTENT_ADD_AREA_ENTITY = "BermudaAddAreaEntity"
INTENT_REMOVE_AREA_ENTITY = "BermudaRemoveAreaEntity"
INTENT_LIST_DEVICES = "BermudaListDevices"

MUTABLE_OPTIONS = {
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_DEVTRACK_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    CONF_SMOOTHING_SAMPLES,
    CONF_ATTENUATION,
    CONF_REF_POWER,
    CONF_AREA_ENTITY_DISTANCE,
}

OPTION_TYPES: dict[str, type] = {
    CONF_MAX_RADIUS: float,
    CONF_MAX_VELOCITY: float,
    CONF_DEVTRACK_TIMEOUT: int,
    CONF_UPDATE_INTERVAL: float,
    CONF_SMOOTHING_SAMPLES: int,
    CONF_ATTENUATION: float,
    CONF_REF_POWER: float,
    CONF_AREA_ENTITY_DISTANCE: float,
}

OPTION_ZH: dict[str, str] = {
    CONF_MAX_RADIUS: "区域检测最大半径",
    CONF_MAX_VELOCITY: "最大移动速度",
    CONF_DEVTRACK_TIMEOUT: "离家判定超时",
    CONF_UPDATE_INTERVAL: "传感器更新间隔",
    CONF_SMOOTHING_SAMPLES: "距离平滑采样数",
    CONF_ATTENUATION: "环境衰减系数",
    CONF_REF_POWER: "参考信号强度",
    CONF_AREA_ENTITY_DISTANCE: "虚拟竞争距离",
    CONF_AREA_ENTITIES: "区域指示实体",
    CONF_DEVICES: "追踪设备列表",
}


def _get_coordinator(hass: HomeAssistant):
    """从 hass 中获取 Bermuda coordinator 实例."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state.name == "LOADED" and hasattr(entry, "runtime_data"):
            return entry.runtime_data.coordinator
    return None


def _err_response(intent_obj: intent.Intent, msg: str) -> intent.IntentResponse:
    """Create an error response with consistent formatting."""
    response = intent_obj.create_response()
    response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, msg)
    return response


def _validate_address(address: str) -> str | None:
    """Validate MAC or iBeacon address format. Returns error message or None."""
    if _MAC_RE.match(address):
        return None
    if _IBEACON_RE.match(address):
        return None
    if len(address) == 12 and all(c in '0123456789ABCDEF' for c in address):
        return f"MAC '{address}' looks like it's missing colons. Use format AA:BB:CC:DD:EE:FF."
    return (
        f"Invalid address format: '{address}'. "
        f"Expected MAC (AA:BB:CC:DD:EE:FF) or iBeacon UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx_major_minor)."
    )


def _hot_reload(coordinator) -> None:
    coordinator.hass.config_entries.async_update_entry(
        coordinator.config_entry, options=dict(coordinator.options)
    )


class BermudaGetConfigHandler(intent.IntentHandler):
    """Query Bermuda BLE trilateration configuration."""

    intent_type = INTENT_GET_CONFIG
    description = (
        "Retrieve Bermuda BLE indoor positioning system configuration. "
        "Bermuda uses Bluetooth Low Energy (BLE) RSSI signals from multiple scanners "
        "(ESPHome proxies, Shelly devices) to trilaterate device positions and determine "
        "which Home Assistant area a tracked device is in. "
        "Call without 'key' to get a full config overview. "
        "Call with 'key' to get a specific parameter value. "
        "Available keys: max_area_radius (max detection radius in meters), "
        "max_velocity (max movement speed m/s, filters outliers), "
        "devtracker_nothome_timeout (seconds before marking device away), "
        "update_interval (sensor refresh rate in seconds), "
        "smoothing_samples (number of RSSI samples for distance averaging), "
        "attenuation (environment RF attenuation factor, higher=more walls), "
        "ref_power (RSSI at 1 meter reference distance, typically -55), "
        "area_entities (entity IDs used as area presence indicators), "
        "configured_devices (tracked BLE device addresses). "
        "Example: user asks 'what are the Bermuda settings' or 'show bluetooth positioning config'."
    )
    slot_schema = {
        vol.Optional("key", description="Config key name, e.g. 'attenuation' or 'ref_power'. Omit to return all config."): str,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return _err_response(intent_obj, _ERR_NOT_LOADED)

        key = intent_obj.slots.get("key", {}).get("value")
        opts = coordinator.options

        response = intent_obj.create_response()
        if key:
            if key not in opts:
                available = ', '.join(sorted(opts.keys()))
                response.async_set_speech(
                    f"**Unknown config key:** '{key}'\n\n"
                    f"Available keys: {available}"
                )
            else:
                zh = OPTION_ZH.get(key, key)
                response.async_set_speech(f"{zh} = {opts[key]}")
        else:
            lines = []
            for k, v in opts.items():
                zh = OPTION_ZH.get(k, k)
                if isinstance(v, list):
                    lines.append(f"- {zh}: {len(v)} 项")
                else:
                    lines.append(f"- {zh}: {v}")
            response.async_set_speech("**Bermuda 当前配置**\n\n" + "\n".join(lines))
        return response


class BermudaSetConfigHandler(intent.IntentHandler):
    """Modify Bermuda config with hot-reload."""

    intent_type = INTENT_SET_CONFIG
    description = (
        "Dynamically modify Bermuda BLE positioning config. Changes take effect immediately "
        "via hot-reload without restarting. "
        "Mutable keys and their types: "
        "max_area_radius (float, meters) - max distance to consider a device in an area, "
        "max_velocity (float, m/s) - ignore jumps faster than this to filter BLE noise, "
        "devtracker_nothome_timeout (int, seconds) - how long before device is marked 'away', "
        "update_interval (float, seconds) - how often sensors refresh, "
        "smoothing_samples (int) - number of RSSI readings to average for stable distance, "
        "attenuation (float) - RF path loss exponent, increase for walls/furniture (typical 2-4), "
        "ref_power (float, dBm) - expected RSSI at exactly 1 meter, usually around -55. "
        "Example: user says 'set Bermuda attenuation to 3.5' → key='attenuation', value='3.5'. "
        "Example: user says 'change positioning update interval to 2 seconds' → key='update_interval', value='2'."
    )
    slot_schema = {
        vol.Required("key", description="Config key to modify, e.g. 'attenuation', 'ref_power', 'max_area_radius'"): intent.non_empty_string,
        vol.Required("value", description="New value as string, e.g. '3.5' or '10'"): intent.non_empty_string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return _err_response(intent_obj, _ERR_NOT_LOADED)

        slots = self.async_validate_slots(intent_obj.slots)
        key = slots["key"]["value"]
        raw_value = slots["value"]["value"]

        response = intent_obj.create_response()
        if key not in MUTABLE_OPTIONS:
            response.async_set_speech(
                f"**Immutable config key:** '{key}'\n\n"
                f"Mutable keys: {', '.join(sorted(MUTABLE_OPTIONS))}\n\n"
                f"Use BermudaExecute for advanced modifications."
            )
            return response

        cast_fn = OPTION_TYPES.get(key, str)
        try:
            value = cast_fn(raw_value)
        except (ValueError, TypeError):
            response.async_set_speech(
                f"**Type error:** cannot convert '{raw_value}' to {cast_fn.__name__}.\n\n"
                f"Key '{key}' expects type {cast_fn.__name__}. Example: '3.5' for float, '10' for int."
            )
            return response

        if isinstance(value, (int, float)) and value < 0:
            response.async_set_speech(f"**Warning:** negative value {value} for '{key}' may cause unexpected behavior.")

        old = coordinator.options.get(key)
        coordinator.options[key] = value
        try:
            _hot_reload(coordinator)
        except Exception as exc:
            coordinator.options[key] = old
            _LOGGER.error("BermudaSetConfig hot-reload failed: %s", exc)
            response.async_set_speech(f"**Hot-reload failed, reverted to previous value.**\n\nError: {exc}")
            return response

        zh = OPTION_ZH.get(key, key)
        response.async_set_speech(f"**Modified** {zh}: {old} → {value}\n\n*Hot-reloaded, effective immediately.*")
        return response


class BermudaExecuteHandler(intent.IntentHandler):
    """Execute Python code on Bermuda coordinator runtime — the super-ability."""

    intent_type = INTENT_EXECUTE
    description = (
        "Execute arbitrary Python code against the live Bermuda BLE positioning runtime. "
        "This is the most powerful Bermuda intent — it gives full read/write access to the "
        "coordinator's internal state, the HA Bluetooth manager, device data, and scanner data. "
        "The code runs with these pre-bound locals: "
        "  coordinator — BermudaDataUpdateCoordinator (the core positioning engine), "
        "  hass — HomeAssistant instance, "
        "  bt_manager — HA BluetoothManager (habluetooth core), "
        "  devices — dict[str, BermudaDevice] keyed by MAC address, "
        "  options — coordinator.options dict (mutable config, call _hot_reload() to persist), "
        "  scanners — set of BermudaDevice objects that are BLE scanners, "
        "  _hot_reload — function to persist options changes to config entry immediately, "
        "  bt_last_service_info(hass, address, connectable=True) — get last BLE advertisement for a MAC, "
        "  bt_discovered(hass, connectable=True) — iterate all discovered BLE service infos, "
        "  bt_address_present(hass, address, connectable=True) — check if MAC is currently visible, "
        "  bt_ble_device(hass, address, connectable=True) — get BLEDevice object for a MAC, "
        "  bt_scanner_count(hass, connectable=True) — count of active BLE scanners, "
        "  monotonic — time.monotonic for timestamp calculations. "
        "The code MUST assign a string to variable 'result' — that string is returned as speech. "
        "EXAMPLES: "
        "1. Query device positions: "
        "   code=\"lines=[f'{d.name}: {d.area_name or \"unknown\"} ({d.area_distance or 0:.1f}m)' "
        "   for d in devices.values() if d.create_sensor]\\nresult='\\n'.join(lines) or 'no devices'\" "
        "2. Inspect a device's raw RSSI per scanner: "
        "   code=\"d=next((d for d in devices.values() if 'phone' in d.name.lower()),None)\\n"
        "   if d:\\n  lines=[f'{a.name}: rssi={a.rssi}, dist={a.rssi_distance}' for a in d.adverts.values()]\\n"
        "  result='\\n'.join(lines)\\nelse:\\n  result='not found'\" "
        "3. List scanners with age: "
        "   code=\"now=monotonic()\\n"
        "   lines=[f'{s.name} [{s.address}] age={now-s.last_seen:.0f}s area={s.area_name}' for s in scanners]\\n"
        "   result='\\n'.join(lines) or 'no scanners'\" "
        "4. Check if a specific MAC is visible on the BLE network: "
        "   code=\"present=bt_address_present(hass,'AA:BB:CC:DD:EE:FF',connectable=False)\\n"
        "   result=f'present={present}'\" "
        "5. Get raw BLE advertisement data for a MAC: "
        "   code=\"si=bt_last_service_info(hass,'AA:BB:CC:DD:EE:FF',connectable=False)\\n"
        "   result=f'rssi={si.rssi} name={si.name} source={si.source}' if si else 'not found'\" "
        "6. Force a full data refresh cycle: "
        "   code=\"coordinator._async_update_data_internal()\\nresult='refresh triggered'\" "
        "7. Modify config and hot-reload: "
        "   code=\"options['attenuation']=3.5\\n_hot_reload()\\nresult='done'\" "
        "8. Count all active BLE devices seen recently: "
        "   code=\"result=str(coordinator.count_active_devices())+' active devices'\" "
        "SAFETY: runs in the HA event loop — avoid blocking I/O or long-running loops. "
        "Use this intent for any data query or action beyond what other Bermuda intents provide."
    )
    slot_schema = {
        vol.Required("code", description=(
            "Python code to execute. Must assign to 'result' variable. "
            "Pre-bound locals: coordinator, hass, bt_manager, devices, options, scanners, "
            "_hot_reload, bt_last_service_info, bt_discovered, bt_address_present, "
            "bt_ble_device, bt_scanner_count, monotonic. "
            "Example: \"result=str(len(devices))+' devices total'\""
        )): intent.non_empty_string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return _err_response(intent_obj, _ERR_NOT_LOADED)

        slots = self.async_validate_slots(intent_obj.slots)
        code = slots["code"]["value"]

        response = intent_obj.create_response()

        if len(code) > _MAX_CODE_LEN:
            response.async_set_speech(
                f"**Code too long:** {len(code)} chars (max {_MAX_CODE_LEN}).\n\n"
                f"Split into smaller operations or use multiple calls."
            )
            return response

        try:
            compiled = compile(code, "<bermuda_intent>", "exec")
        except SyntaxError as exc:
            response.async_set_speech(
                f"**Syntax error at line {exc.lineno}:**\n\n"
                f"```\n{exc.msg}\n```\n\n"
                f"Offending text: `{exc.text.strip() if exc.text else 'N/A'}`"
            )
            return response

        exec_locals: dict = {
            "coordinator": coordinator,
            "hass": hass,
            "bt_manager": coordinator._manager,
            "devices": coordinator.devices,
            "options": coordinator.options,
            "scanners": coordinator.get_scanners,
            "_hot_reload": lambda: _hot_reload(coordinator),
            "bt_last_service_info": async_last_service_info,
            "bt_discovered": async_discovered_service_info,
            "bt_address_present": async_address_present,
            "bt_ble_device": async_ble_device_from_address,
            "bt_scanner_count": async_scanner_count,
            "monotonic": monotonic,
            "result": None,
        }

        try:
            exec(compiled, {"__builtins__": __builtins__}, exec_locals)  # noqa: S102
            result = exec_locals.get("result")
            if result is None:
                response.async_set_speech(
                    "**Code executed successfully** but variable `result` was not assigned.\n\n"
                    "Your code must set `result = 'some string'` to return output."
                )
            else:
                out = str(result)
                if len(out) > 2000:
                    out = out[:2000] + f"\n\n... (truncated, {len(str(result))} chars total)"
                response.async_set_speech(out)
        except Exception as exc:
            _LOGGER.warning("BermudaExecute runtime error: %s", exc, exc_info=True)
            response.async_set_speech(
                f"**Runtime error ({type(exc).__name__}):**\n\n"
                f"```\n{exc}\n```\n\n"
                f"Check HA logs for full traceback."
            )
        return response


class BermudaManageDeviceHandler(intent.IntentHandler):
    """Dynamically add/remove tracked BLE MAC addresses — hot-reload into the positioning pipeline."""

    intent_type = INTENT_MANAGE_DEVICE
    description = (
        "Dynamically add or remove a BLE device MAC address from Bermuda's tracked device list. "
        "Changes are hot-reloaded: the MAC immediately enters/exits the BLE trilateration pipeline "
        "on the next update cycle (typically within 1-2 seconds), with no restart needed. "
        "HOW BERMUDA TRACKING WORKS: "
        "Every update cycle, Bermuda reads options.configured_devices (a list of MAC addresses), "
        "calls _get_or_create_device() for each, which creates a BermudaDevice entry in the "
        "coordinator.devices dict. The device then receives RSSI advertisements from all nearby "
        "BLE scanners, calculates smoothed distances, and determines which area it is in. "
        "ACTION 'add': Adds a MAC to configured_devices and creates the device entry immediately. "
        "The MAC will start receiving scanner data on the next update cycle. "
        "ACTION 'remove': Removes a MAC from configured_devices. The device entry will be pruned "
        "on the next prune cycle. "
        "ACTION 'list': Lists all currently configured device MACs and their tracking status. "
        "MAC FORMAT: Use colon-separated uppercase, e.g. 'AA:BB:CC:DD:EE:FF'. "
        "Also supports iBeacon UUIDs in format 'uuid_major_minor'. "
        "Example: user says 'add my new watch to bluetooth tracking' → action='add', address='AA:BB:CC:DD:EE:FF'. "
        "Example: user says 'stop tracking the old beacon' → action='remove', address='XX:XX:XX:XX:XX:XX'. "
        "Example: user says 'what MACs is Bermuda tracking' → action='list'."
    )
    slot_schema = {
        vol.Required("action", description="One of: 'add', 'remove', 'list'"): intent.non_empty_string,
        vol.Optional("address", description="BLE MAC address (AA:BB:CC:DD:EE:FF) or iBeacon UUID. Required for add/remove."): str,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return _err_response(intent_obj, _ERR_NOT_LOADED)

        slots = self.async_validate_slots(intent_obj.slots)
        action = slots["action"]["value"].strip().lower()
        address_raw = slots.get("address", {}).get("value", "").strip()
        address = address_raw.upper()

        valid_actions = ("add", "remove", "list")
        if action not in valid_actions:
            response = intent_obj.create_response()
            response.async_set_speech(
                f"**Invalid action:** '{action}'\n\n"
                f"Valid actions: {', '.join(valid_actions)}\n\n"
                f"Examples:\n"
                f"- action='add', address='AA:BB:CC:DD:EE:FF'\n"
                f"- action='remove', address='AA:BB:CC:DD:EE:FF'\n"
                f"- action='list' (no address needed)"
            )
            return response

        current: list[str] = list(coordinator.options.get(CONF_DEVICES, []))
        response = intent_obj.create_response()

        if action == "list":
            if not current:
                response.async_set_speech(
                    "**No devices configured for tracking.**\n\n"
                    "Use action='add' with a BLE MAC address to start tracking a device.\n"
                    "Example: action='add', address='AA:BB:CC:DD:EE:FF'"
                )
                return response
            lines = []
            for mac in current:
                dev = coordinator._get_device(mac)
                if dev and dev.create_sensor:
                    area = dev.area_name or "unknown"
                    dist = f"{dev.area_distance:.1f}m" if dev.area_distance is not None else "-"
                    seen_ago = ""
                    if dev.last_seen > 0:
                        age = monotonic() - dev.last_seen
                        seen_ago = f", last seen {age:.0f}s ago"
                    lines.append(f"- **{mac}** ({dev.name}) → {area} ({dist}{seen_ago})")
                else:
                    lines.append(f"- **{mac}** (awaiting first BLE advertisement)")
            response.async_set_speech(
                f"**Tracking {len(current)} devices:**\n\n" + "\n".join(lines)
            )
            return response

        if not address:
            response.async_set_speech(
                f"**Missing 'address' parameter.**\n\n"
                f"Action '{action}' requires a BLE MAC address.\n"
                f"Format: AA:BB:CC:DD:EE:FF (colon-separated, uppercase)\n"
                f"Or iBeacon: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx_major_minor"
            )
            return response

        addr_err = _validate_address(address)
        if addr_err:
            response.async_set_speech(f"**Address validation failed:**\n\n{addr_err}")
            return response

        if action == "add":
            if address in current:
                dev = coordinator._get_device(address)
                status = "active" if (dev and dev.create_sensor) else "configured but not yet seen"
                response.async_set_speech(
                    f"**{address} is already in the tracking list.**\n\n"
                    f"Status: {status}\n\n"
                    f"Use action='list' to see all tracked devices."
                )
                return response
            current.append(address)
            coordinator.options[CONF_DEVICES] = current
            try:
                _hot_reload(coordinator)
            except Exception as exc:
                current.remove(address)
                coordinator.options[CONF_DEVICES] = current
                _LOGGER.error("BermudaManageDevice add hot-reload failed: %s", exc)
                response.async_set_speech(f"**Hot-reload failed, addition reverted.**\n\nError: {exc}")
                return response
            dev = coordinator._get_or_create_device(address)
            dev.create_sensor = True
            response.async_set_speech(
                f"**Device added to BLE tracking pipeline:**\n\n"
                f"- Address: {address}\n"
                f"- Status: active, awaiting BLE advertisements from nearby scanners\n"
                f"- Pipeline: RSSI collection → distance smoothing → area determination\n\n"
                f"*Hot-reloaded. Device enters trilateration on next update cycle (~1-2s).*"
            )
            return response

        if action == "remove":
            if address not in current:
                similar = [m for m in current if address[:8] in m]
                hint = f"\n\nSimilar addresses in list: {', '.join(similar)}" if similar else ""
                response.async_set_speech(
                    f"**{address} is not in the tracking list.**{hint}\n\n"
                    f"Use action='list' to see all tracked devices."
                )
                return response
            current.remove(address)
            coordinator.options[CONF_DEVICES] = current
            try:
                _hot_reload(coordinator)
            except Exception as exc:
                current.append(address)
                coordinator.options[CONF_DEVICES] = current
                _LOGGER.error("BermudaManageDevice remove hot-reload failed: %s", exc)
                response.async_set_speech(f"**Hot-reload failed, removal reverted.**\n\nError: {exc}")
                return response
            dev = coordinator._get_device(address)
            if dev:
                dev.create_sensor = False
            response.async_set_speech(
                f"**Device removed from tracking:**\n\n"
                f"- Address: {address}\n"
                f"- Sensors will be marked unavailable\n"
                f"- Device entry will be cleaned up on next prune cycle\n\n"
                f"*Hot-reloaded, effective immediately.*"
            )
            return response

        return response


class BermudaAddAreaEntityHandler(intent.IntentHandler):
    """Add an area presence indicator entity."""

    intent_type = INTENT_ADD_AREA_ENTITY
    description = (
        "Add a Home Assistant entity as a Bermuda area presence indicator. "
        "Takes effect immediately via hot-reload. "
        "HOW IT WORKS: When the entity state is 'on' or 'true' (case-insensitive), "
        "Bermuda treats the entity's assigned area as having a person present. "
        "This supplements BLE distance-based area detection. "
        "The entity MUST have an area assigned (directly or via its parent device). "
        "SUITABLE ENTITIES: binary_sensor (motion, door, occupancy), switch, input_boolean. "
        "Example: binary_sensor.living_room_motion → when 'on', living room is occupied. "
        "Example: binary_sensor.front_door_contact → when 'on' (door open), hallway is active. "
        "Example: input_boolean.bedroom_override → manual area presence flag. "
        "Example: user says 'add the kitchen motion sensor to Bermuda' → "
        "entity_id='binary_sensor.kitchen_motion'."
    )
    slot_schema = {
        vol.Required("entity_id", description="Entity ID to add, e.g. 'binary_sensor.living_room_motion' or 'input_boolean.bedroom_override'"): intent.non_empty_string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return _err_response(intent_obj, _ERR_NOT_LOADED)

        slots = self.async_validate_slots(intent_obj.slots)
        entity_id = slots["entity_id"]["value"].strip()

        if "." not in entity_id:
            response = intent_obj.create_response()
            response.async_set_speech(
                f"**Invalid entity_id format:** '{entity_id}'\n\n"
                f"Entity IDs must contain a domain prefix, e.g. binary_sensor.living_room_motion"
            )
            return response

        current: list[str] = list(coordinator.options.get(CONF_AREA_ENTITIES, []))

        response = intent_obj.create_response()
        if entity_id in current:
            response.async_set_speech(
                f"**{entity_id} is already in the area indicator list.**\n\n"
                f"Current list ({len(current)} entities): {', '.join(current)}"
            )
            return response

        state = hass.states.get(entity_id)
        if state is None:
            response.async_set_speech(
                f"**Entity not found:** '{entity_id}'\n\n"
                f"Verify the entity_id exists in HA Developer Tools → States."
            )
            return response

        try:
            area_id, area_name = coordinator.area_entity_manager.resolve_entity_area(entity_id)
        except Exception as exc:
            _LOGGER.error("Failed to resolve area for %s: %s", entity_id, exc)
            response.async_set_speech(f"**Area resolution error:** {exc}")
            return response

        if area_id is None:
            response.async_set_speech(
                f"**Cannot add:** {entity_id} has no area assigned.\n\n"
                f"Go to HA Settings → Devices/Entities → find this entity → assign an area.\n"
                f"The area is inherited from the entity's device if not set directly."
            )
            return response

        current.append(entity_id)
        coordinator.options[CONF_AREA_ENTITIES] = current
        try:
            _hot_reload(coordinator)
        except Exception as exc:
            current.remove(entity_id)
            coordinator.options[CONF_AREA_ENTITIES] = current
            _LOGGER.error("BermudaAddAreaEntity hot-reload failed: %s", exc)
            response.async_set_speech(f"**Hot-reload failed, addition reverted.**\n\nError: {exc}")
            return response

        response.async_set_speech(
            f"**Area indicator added:**\n\n"
            f"- Entity: {entity_id}\n"
            f"- Area: {area_name}\n"
            f"- Current state: {state.state}\n"
            f"- Trigger rule: state 'on'/'true' = area occupied\n\n"
            f"*Hot-reloaded. Active on next update cycle.*"
        )
        return response


class BermudaRemoveAreaEntityHandler(intent.IntentHandler):
    """Remove an area presence indicator entity."""

    intent_type = INTENT_REMOVE_AREA_ENTITY
    description = (
        "Remove an entity from Bermuda's area presence indicator list. "
        "Takes effect immediately via hot-reload. "
        "After removal, the entity's state will no longer influence area detection. "
        "Use BermudaGetConfig to see the current area_entities list first. "
        "Example: user says 'remove the bedroom motion sensor from Bermuda area indicators' → "
        "entity_id='binary_sensor.bedroom_motion'."
    )
    slot_schema = {
        vol.Required("entity_id", description="Entity ID to remove, must be currently in the area_entities list"): intent.non_empty_string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return _err_response(intent_obj, _ERR_NOT_LOADED)

        slots = self.async_validate_slots(intent_obj.slots)
        entity_id = slots["entity_id"]["value"].strip()
        current: list[str] = list(coordinator.options.get(CONF_AREA_ENTITIES, []))

        response = intent_obj.create_response()
        if entity_id not in current:
            response.async_set_speech(
                f"**'{entity_id}' is not in the area indicator list.**\n\n"
                f"Current list ({len(current)} entities): {', '.join(current) if current else '(empty)'}"
            )
            return response

        current.remove(entity_id)
        coordinator.options[CONF_AREA_ENTITIES] = current
        try:
            _hot_reload(coordinator)
        except Exception as exc:
            current.append(entity_id)
            coordinator.options[CONF_AREA_ENTITIES] = current
            _LOGGER.error("BermudaRemoveAreaEntity hot-reload failed: %s", exc)
            response.async_set_speech(f"**Hot-reload failed, removal reverted.**\n\nError: {exc}")
            return response

        remaining = len(current)
        response.async_set_speech(
            f"**Removed area indicator:** {entity_id}\n\n"
            f"Remaining indicators: {remaining}\n\n"
            f"*Hot-reloaded, effective immediately.*"
        )
        return response


class BermudaListDevicesHandler(intent.IntentHandler):
    """List all Bermuda tracked devices."""

    intent_type = INTENT_LIST_DEVICES
    description = (
        "List all BLE devices currently tracked by Bermuda and their detected areas. "
        "Each device shows its friendly name and the Home Assistant area it is currently in. "
        "Bermuda tracks devices configured in its settings — typically BLE beacons, phones, "
        "smartwatches, or any device that broadcasts BLE advertisements. "
        "The area is determined by the nearest BLE scanner's assigned area, "
        "or overridden by area presence indicator entities (motion sensors, etc). "
        "Example: user asks 'what devices is Bermuda tracking' or 'list all bluetooth tracked devices'."
    )

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return _err_response(intent_obj, _ERR_NOT_LOADED)

        lines = []
        for device in coordinator.devices.values():
            if not device.create_sensor:
                continue
            area = device.area_name or "unknown"
            dist = f"{device.area_distance:.1f}m" if device.area_distance is not None else "-"
            lines.append(f"- **{device.name}** [{device.address}] → {area} ({dist})")

        scanner_count = coordinator.count_active_scanners()
        response = intent_obj.create_response()
        if not lines:
            response.async_set_speech(
                f"**No tracked devices.**\n\n"
                f"Active scanners: {scanner_count}\n\n"
                f"Use BermudaManageDevice(action='add', address='AA:BB:CC:DD:EE:FF') to start tracking."
            )
        else:
            response.async_set_speech(
                f"**Tracking {len(lines)} devices** ({scanner_count} active scanners):\n\n" + "\n".join(lines)
            )
        return response


async def async_setup_intents(hass: HomeAssistant) -> None:
    """Register all Bermuda intent handlers."""
    intent.async_register(hass, BermudaGetConfigHandler())
    intent.async_register(hass, BermudaSetConfigHandler())
    intent.async_register(hass, BermudaExecuteHandler())
    intent.async_register(hass, BermudaManageDeviceHandler())
    intent.async_register(hass, BermudaAddAreaEntityHandler())
    intent.async_register(hass, BermudaRemoveAreaEntityHandler())
    intent.async_register(hass, BermudaListDevicesHandler())
    _LOGGER.info("Bermuda: 7 intents registered")
