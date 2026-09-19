#!/usr/bin/env python3
"""A minimal long-polling Telegram bridge for the local Codex CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent
DOTENV_PATH = PROJECT_DIR / ".env"
REPLY_CHUNK_SIZE = 3900


CAR_ACTIONS: dict[str, tuple[str, str]] = {
    "flash": ("Flash lights", "flash"),
    "honk_flash": ("Honk + flash", "honk-and-flash"),
    "lock": ("Lock vehicle", "lock"),
    "unlock": ("Unlock vehicle", "unlock"),
}


class ConfigurationError(ValueError):
    """The bridge cannot start with its current configuration."""


class TelegramAPIError(RuntimeError):
    """Telegram returned an unsuccessful API response."""


class VehicleApiError(RuntimeError):
    """The OpenAPI vehicle service could not complete a request."""

    def __init__(
        self,
        kind: Literal["unavailable", "authentication", "timeout", "invalid", "configuration"],
        message: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind


def load_dotenv(path: Path) -> dict[str, str]:
    """Read the small KEY=VALUE subset needed by this project.

    Environment variables take precedence, making it easy to run under systemd
    or Docker without copying secrets into a file.
    """

    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ConfigurationError(f"Invalid .env entry on line {line_number}.")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigurationError(f"Missing variable name on line {line_number}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def get_setting(dotenv: dict[str, str], name: str, default: str = "") -> str:
    return os.environ.get(name, dotenv.get(name, default)).strip()


def parse_bool(value: str, name: str) -> bool:
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def parse_user_ids(value: str) -> frozenset[int]:
    if not value:
        return frozenset()
    try:
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ConfigurationError("TELEGRAM_ALLOWED_USER_IDS must be comma-separated numeric IDs.") from exc


def format_bytes(value: int) -> str:
    """Format a byte count for a compact Telegram response."""

    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


def linux_memory_info() -> tuple[int, int] | None:
    """Return total and available memory from /proc, when the host exposes it."""

    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value, *_ = line.replace(":", "").split()
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value) * 1024
        return values["MemTotal"], values["MemAvailable"]
    except (FileNotFoundError, KeyError, ValueError):
        return None


def linux_cpu_times() -> tuple[int, int] | None:
    """Return aggregate CPU total and idle ticks from /proc/stat."""

    try:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        if fields[0] != "cpu":
            return None
        values = [int(value) for value in fields[1:]]
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    except (FileNotFoundError, IndexError, ValueError):
        return None


def cpu_usage_percent() -> float | None:
    """Sample Linux aggregate CPU utilization over a short interval."""

    first = linux_cpu_times()
    if first is None:
        return None
    time.sleep(0.15)
    second = linux_cpu_times()
    if second is None:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, 100 * (1 - idle_delta / total_delta)))


def cpu_temperature_celsius() -> float | None:
    """Read the first plausible CPU temperature exposed by Linux thermal sensors."""

    hwmon_paths = sorted(Path("/sys/class/hwmon").glob("hwmon*/temp*_input"))
    cpu_drivers = {"coretemp", "k10temp", "zenpower", "cpu_thermal"}
    preferred_hwmon_paths: list[Path] = []
    other_hwmon_paths: list[Path] = []
    for path in hwmon_paths:
        try:
            driver = (path.parent / "name").read_text(encoding="utf-8").strip().lower()
        except OSError:
            driver = ""
        (preferred_hwmon_paths if driver in cpu_drivers else other_hwmon_paths).append(path)

    # Most x86 and ARM machines identify CPU sensors through a hwmon driver.
    # Retain other thermal readings as a best-effort fallback for simpler hosts.
    sensor_paths = preferred_hwmon_paths + sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    sensor_paths.extend(other_hwmon_paths)
    for path in sensor_paths:
        try:
            value = float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        # Kernel interfaces normally report millidegrees, though a few report degrees.
        value = value / 1000 if value > 1_000 else value
        if 0 < value < 150:
            return value
    return None


def server_summary() -> str:
    """Build a best-effort host health summary without third-party packages."""

    cpu_usage = cpu_usage_percent()
    memory = linux_memory_info()
    temperature = cpu_temperature_celsius()
    storage = shutil.disk_usage("/")

    lines = [
        "Server status",
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"CPU usage: {f'{cpu_usage:.1f}%' if cpu_usage is not None else 'unavailable'}",
    ]
    if memory is None:
        lines.extend(["RAM usage: unavailable", "Available memory: unavailable"])
    else:
        total_memory, available_memory = memory
        used_memory = total_memory - available_memory
        lines.extend(
            [
                f"RAM usage: {format_bytes(used_memory)} / {format_bytes(total_memory)} "
                f"({used_memory / total_memory * 100:.1f}%)",
                f"Available memory: {format_bytes(available_memory)}",
            ]
        )
    lines.extend(
        [
            f"CPU temperature: {f'{temperature:.1f} °C' if temperature is not None else 'unavailable'}",
            f"Available storage (/): {format_bytes(storage.free)} / {format_bytes(storage.total)} "
            f"({storage.free / storage.total * 100:.1f}% free)",
        ]
    )
    return "\n".join(lines)


def optional_text(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"Car response field {field!r} was not text.")
    return value.strip() or None


def optional_number(
    payload: dict[str, Any], field: str, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"Car response field {field!r} was not numeric.")
    number = float(value)
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise RuntimeError(f"Car response field {field!r} was outside its valid range.")
    return number


def compact_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


@dataclass(frozen=True)
class CarSnapshot:
    vin: str | None
    vehicle_name: str | None
    license_plate: str | None
    vehicle_title: str | None
    car_type: str | None
    engine_type: str | None
    total_range_km: float | None
    range_km: float | None
    battery_percent: float | None
    doors_locked: str | None
    locked: str | None
    doors: str | None
    windows: str | None
    reliable_lock_status: str | None
    sunroof: str | None
    trunk: str | None
    bonnet: str | None
    lights: str | None
    air_conditioning_state: str | None
    air_conditioning_temperature: float | None
    air_conditioning_temperature_unit: str | None
    charging_state: str | None
    charging_rate_kmh: float | None
    charging_power_kw: float | None
    charging_remaining_minutes: float | None
    charging_type: str | None
    charging_range_m: float | None
    charging_battery_percent: float | None
    last_charging_start: str | None
    last_charging_kwh: float | None
    last_charging_duration_minutes: float | None
    last_charging_current_type: str | None
    location_country: str | None
    location_county: str | None
    location_address: str | None
    latitude: float | None
    longitude: float | None
    updated_at: str | None
    partial: bool = False
    unavailable_sections: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CarSnapshot":
        location = payload.get("location")
        if location is not None and not isinstance(location, dict):
            raise RuntimeError("Car response field 'location' was not an object.")
        location = location or {}
        unavailable = payload.get("unavailableSections", payload.get("unavailable_sections", []))
        if not isinstance(unavailable, list) or not all(isinstance(item, str) for item in unavailable):
            raise RuntimeError("Car response field 'unavailableSections' was invalid.")
        partial = payload.get("partial", False)
        if not isinstance(partial, bool):
            raise RuntimeError("Car response field 'partial' was not boolean.")

        def location_text(field: str) -> str | None:
            source = location if field in location else payload
            return optional_text(source, field)

        def location_number(field: str, minimum: float, maximum: float) -> float | None:
            source = location if field in location else payload
            return optional_number(source, field, minimum, maximum)

        return cls(
            vin=optional_text(payload, "vin"),
            vehicle_name=optional_text(payload, "vehicleName") if "vehicleName" in payload else optional_text(payload, "vehicle_name"),
            license_plate=optional_text(payload, "licensePlate") if "licensePlate" in payload else optional_text(payload, "license_plate"),
            vehicle_title=optional_text(payload, "vehicleTitle") if "vehicleTitle" in payload else optional_text(payload, "vehicle_title"),
            car_type=optional_text(payload, "carType") if "carType" in payload else optional_text(payload, "car_type"),
            engine_type=optional_text(payload, "engineType") if "engineType" in payload else optional_text(payload, "engine_type"),
            total_range_km=optional_number(payload, "totalRangeKm", 0) if "totalRangeKm" in payload else optional_number(payload, "total_range_km", 0),
            range_km=optional_number(payload, "rangeKm", 0) if "rangeKm" in payload else optional_number(payload, "range_km", 0),
            battery_percent=optional_number(payload, "batteryPercent", 0, 100) if "batteryPercent" in payload else optional_number(payload, "battery_percent", 0, 100),
            doors_locked=optional_text(payload, "doorsLocked") if "doorsLocked" in payload else optional_text(payload, "doors_locked"),
            locked=optional_text(payload, "locked"),
            doors=optional_text(payload, "doors"),
            windows=optional_text(payload, "windows"),
            reliable_lock_status=optional_text(payload, "reliableLockStatus") if "reliableLockStatus" in payload else optional_text(payload, "reliable_lock_status"),
            sunroof=optional_text(payload, "sunroof"),
            trunk=optional_text(payload, "trunk"),
            bonnet=optional_text(payload, "bonnet"),
            lights=optional_text(payload, "lights"),
            air_conditioning_state=optional_text(payload, "airConditioningState") if "airConditioningState" in payload else optional_text(payload, "air_conditioning_state"),
            air_conditioning_temperature=optional_number(payload, "airConditioningTemperature") if "airConditioningTemperature" in payload else optional_number(payload, "air_conditioning_temperature"),
            air_conditioning_temperature_unit=optional_text(payload, "airConditioningTemperatureUnit") if "airConditioningTemperatureUnit" in payload else optional_text(payload, "air_conditioning_temperature_unit"),
            charging_state=optional_text(payload, "chargingState") if "chargingState" in payload else optional_text(payload, "charging_state"),
            charging_rate_kmh=optional_number(payload, "chargingRateKmh", 0) if "chargingRateKmh" in payload else optional_number(payload, "charging_rate_kmh", 0),
            charging_power_kw=optional_number(payload, "chargingPowerKw", 0) if "chargingPowerKw" in payload else optional_number(payload, "charging_power_kw", 0),
            charging_remaining_minutes=optional_number(payload, "chargingRemainingMinutes", 0) if "chargingRemainingMinutes" in payload else optional_number(payload, "charging_remaining_minutes", 0),
            charging_type=optional_text(payload, "chargingType") if "chargingType" in payload else optional_text(payload, "charging_type"),
            charging_range_m=optional_number(payload, "chargingRangeM", 0) if "chargingRangeM" in payload else optional_number(payload, "charging_range_m", 0),
            charging_battery_percent=optional_number(payload, "chargingBatteryPercent", 0, 100) if "chargingBatteryPercent" in payload else optional_number(payload, "charging_battery_percent", 0, 100),
            last_charging_start=optional_text(payload, "lastChargingStart") if "lastChargingStart" in payload else optional_text(payload, "last_charging_start"),
            last_charging_kwh=optional_number(payload, "lastChargingKwh", 0) if "lastChargingKwh" in payload else optional_number(payload, "last_charging_kwh", 0),
            last_charging_duration_minutes=optional_number(payload, "lastChargingDurationMinutes", 0) if "lastChargingDurationMinutes" in payload else optional_number(payload, "last_charging_duration_minutes", 0),
            last_charging_current_type=optional_text(payload, "lastChargingCurrentType") if "lastChargingCurrentType" in payload else optional_text(payload, "last_charging_current_type"),
            location_country=location_text("country"),
            location_county=location_text("county"),
            location_address=location_text("address"),
            latitude=location_number("latitude", -90, 90),
            longitude=location_number("longitude", -180, 180),
            updated_at=optional_text(payload, "capturedAt") if "capturedAt" in payload else optional_text(payload, "updated_at"),
            partial=partial,
            unavailable_sections=tuple(unavailable),
        )

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def format(self) -> str:
        title = self.vehicle_name or "Configured vehicle"
        if self.vehicle_title and self.vehicle_title != title:
            title += f" — {self.vehicle_title}"
        if self.license_plate:
            title += f" ({self.license_plate})"
        lines = [f"🚙 {title}"]

        range_parts: list[str] = []
        if self.range_km is not None:
            range_parts.append(f"Range: {compact_number(self.range_km)} km")
        if self.battery_percent is not None:
            range_parts.append(f"Battery: {compact_number(self.battery_percent)}%")
        lines.append("🔋 " + (" • ".join(range_parts) if range_parts else "Range unavailable"))
        if self.total_range_km is not None or self.car_type or self.engine_type:
            range_details: list[str] = []
            if self.total_range_km is not None:
                range_details.append(f"Total: {compact_number(self.total_range_km)} km")
            if self.car_type:
                range_details.append(f"Type: {self.car_type}")
            if self.engine_type:
                range_details.append(f"Engine: {self.engine_type}")
            lines.append(" • ".join(range_details))
        lines.append(f"🔐 Lock: {self.doors_locked or self.locked or 'Unavailable'}")
        lines.append(f"🚪 Doors: {self.doors or 'Unavailable'}")
        if self.windows:
            lines.append(f"🪟 Windows: {self.windows}")
        if self.reliable_lock_status:
            lines.append(f"Lock reliability: {self.reliable_lock_status}")
        if self.sunroof:
            lines.append(f"Sunroof: {self.sunroof}")
        if self.trunk:
            lines.append(f"🧳 Trunk: {self.trunk}")
        if self.bonnet:
            lines.append(f"Bonnet: {self.bonnet}")
        if self.lights:
            lines.append(f"💡 Lights: {self.lights}")
        if self.air_conditioning_state:
            climate = f"❄️ Air conditioning: {self.air_conditioning_state}"
            if self.air_conditioning_temperature is not None:
                climate += f" ({compact_number(self.air_conditioning_temperature)} {self.air_conditioning_temperature_unit or '°'})"
            lines.append(climate)
        if self.charging_state:
            charging = f"⚡ Charging: {self.charging_state}"
            if self.charging_battery_percent is not None:
                charging += f" ({compact_number(self.charging_battery_percent)}%)"
            if self.charging_range_m is not None:
                charging += f", {compact_number(self.charging_range_m / 1000)} km"
            lines.append(charging)
            charging_details: list[str] = []
            if self.charging_rate_kmh is not None:
                charging_details.append(f"{compact_number(self.charging_rate_kmh)} km/h")
            if self.charging_power_kw is not None:
                charging_details.append(f"{compact_number(self.charging_power_kw)} kW")
            if self.charging_remaining_minutes is not None:
                charging_details.append(f"{compact_number(self.charging_remaining_minutes)} min remaining")
            if self.charging_type:
                charging_details.append(self.charging_type)
            if charging_details:
                lines.append(" • ".join(charging_details))
        if self.last_charging_start:
            session = f"Last charge: {self.last_charging_start}"
            session_details: list[str] = []
            if self.last_charging_kwh is not None:
                session_details.append(f"{compact_number(self.last_charging_kwh)} kWh")
            if self.last_charging_duration_minutes is not None:
                session_details.append(f"{compact_number(self.last_charging_duration_minutes)} min")
            if self.last_charging_current_type:
                session_details.append(self.last_charging_current_type)
            if session_details:
                session += " (" + " • ".join(session_details) + ")"
            lines.append(session)
        if self.location_country:
            lines.append(f"Country: {self.location_country}")
        if self.location_county:
            lines.append(f"County: {self.location_county}")
        if self.location_address:
            lines.append(f"Address: {self.location_address}")
        if self.has_coordinates:
            assert self.latitude is not None and self.longitude is not None
            lines.append(f"Coordinates: {self.latitude:.6f}, {self.longitude:.6f}")
        elif not self.location_address and not self.location_country and not self.location_county:
            lines.append("Location unavailable")
        if self.updated_at:
            lines.append(f"Updated: {self.updated_at}")
        if self.partial:
            unavailable = ", ".join(self.unavailable_sections) or "some vehicle data"
            lines.append(f"⚠️ Unavailable: {unavailable}")
        return "\n".join(lines)


def car_action_keyboard() -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Refresh", "callback_data": "car:refresh"}
            ],
            [
                {"text": "💡 Flash lights", "callback_data": "car:confirm:flash"},
                {"text": "📣 Honk + flash", "callback_data": "car:confirm:honk_flash"},
            ],
            [
                {"text": "🔒 Lock", "callback_data": "car:confirm:lock"},
                {"text": "🔓 Unlock", "callback_data": "car:confirm:unlock"},
            ],
        ]
    }


def car_confirmation_keyboard(action: str) -> dict[str, list[list[dict[str, str]]]]:
    label, _ = CAR_ACTIONS[action]
    return {
        "inline_keyboard": [
            [{"text": f"Confirm: {label}", "callback_data": f"car:run:{action}"}],
            [{"text": "Cancel", "callback_data": "car:cancel"}],
        ]
    }


@dataclass(frozen=True)
class CodexProfile:
    model: str | None
    reasoning_effort: str | None
    timeout_seconds: int


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    codex_bin: str
    codex_workdir: Path
    projects_dir: Path
    wiki_workdir: Path
    codex_model: str | None
    codex_unsafe_mode: bool
    codex_timeout_seconds: int
    fast_profile: CodexProfile
    default_profile: CodexProfile
    deep_profile: CodexProfile
    skoda_gateway_url: str
    skoda_gateway_token: str
    skoda_request_timeout_seconds: int
    skoda_snapshot_cache_seconds: float
    skoda_action_cooldown_seconds: float

    @classmethod
    def load(cls) -> "Settings":
        dotenv = load_dotenv(DOTENV_PATH)
        token = get_setting(dotenv, "TELEGRAM_BOT_TOKEN")
        if not token or token == "PASTE_TOKEN_FROM_BOTFATHER":
            raise ConfigurationError("Set TELEGRAM_BOT_TOKEN in .env before starting the bridge.")

        workdir = Path(get_setting(dotenv, "CODEX_WORKDIR", str(PROJECT_DIR))).expanduser()
        if not workdir.is_dir():
            raise ConfigurationError(f"CODEX_WORKDIR does not exist or is not a directory: {workdir}")
        projects_dir = Path(get_setting(dotenv, "PROJECTS_DIR", "~/Projects")).expanduser()
        wiki_workdir = Path(
            get_setting(dotenv, "WIKI_WORKDIR", str(projects_dir / "llm-wiki"))
        ).expanduser()
        if not wiki_workdir.is_dir():
            raise ConfigurationError(f"WIKI_WORKDIR does not exist or is not a directory: {wiki_workdir}")

        timeout_raw = get_setting(dotenv, "CODEX_TIMEOUT_SECONDS", "900")
        try:
            timeout = int(timeout_raw)
        except ValueError as exc:
            raise ConfigurationError("CODEX_TIMEOUT_SECONDS must be a whole number.") from exc
        if timeout < 1:
            raise ConfigurationError("CODEX_TIMEOUT_SECONDS must be greater than zero.")

        model = get_setting(dotenv, "CODEX_MODEL") or None
        def profile(prefix: str, fallback_model: str | None, fallback_reasoning: str) -> CodexProfile:
            model_value = get_setting(dotenv, f"CODEX_{prefix}_MODEL") or fallback_model
            reasoning = get_setting(dotenv, f"CODEX_{prefix}_REASONING", fallback_reasoning)
            if reasoning not in {"none", "low", "medium", "high", "xhigh"}:
                raise ConfigurationError(f"CODEX_{prefix}_REASONING must be a supported reasoning effort.")
            return CodexProfile(model_value, reasoning, timeout)

        request_timeout_raw = get_setting(dotenv, "SKODA_REQUEST_TIMEOUT_SECONDS", "20")
        cache_raw = get_setting(dotenv, "SKODA_SNAPSHOT_CACHE_SECONDS", "5")
        cooldown_raw = get_setting(dotenv, "SKODA_ACTION_COOLDOWN_SECONDS", "15")
        try:
            request_timeout = int(request_timeout_raw)
            cache_seconds = float(cache_raw)
            cooldown_seconds = float(cooldown_raw)
        except ValueError as exc:
            raise ConfigurationError("SKODA timeout, cache, and cooldown values must be numeric.") from exc
        if request_timeout < 1 or cache_seconds < 0 or cooldown_seconds < 0:
            raise ConfigurationError("SKODA timeout must be positive; cache and cooldown cannot be negative.")
        gateway_url = get_setting(dotenv, "SKODA_GATEWAY_URL", "http://127.0.0.1:8091").rstrip("/")
        gateway_token = get_setting(dotenv, "SKODA_GATEWAY_TOKEN")
        if not gateway_token:
            raise ConfigurationError("Set SKODA_GATEWAY_TOKEN in the environment before using /car.")

        return cls(
            telegram_token=token,
            allowed_user_ids=parse_user_ids(get_setting(dotenv, "TELEGRAM_ALLOWED_USER_IDS")),
            codex_bin=get_setting(dotenv, "CODEX_BIN", "codex"),
            codex_workdir=workdir.resolve(),
            projects_dir=projects_dir.resolve(),
            wiki_workdir=wiki_workdir.resolve(),
            codex_model=model,
            codex_unsafe_mode=parse_bool(get_setting(dotenv, "CODEX_UNSAFE_MODE", "false"), "CODEX_UNSAFE_MODE"),
            codex_timeout_seconds=timeout,
            fast_profile=profile("FAST", model, "low"),
            default_profile=profile("DEFAULT", model, "medium"),
            deep_profile=profile("DEEP", model, "high"),
            skoda_gateway_url=gateway_url,
            skoda_gateway_token=gateway_token,
            skoda_request_timeout_seconds=request_timeout,
            skoda_snapshot_cache_seconds=cache_seconds,
            skoda_action_cooldown_seconds=cooldown_seconds,
        )


class TelegramBot:
    def __init__(self, token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        def request() -> Any:
            body = json.dumps(payload).encode("utf-8")
            http_request = Request(
                f"{self._base_url}/{method}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(http_request, timeout=35) as response:  # noqa: S310 - fixed Telegram endpoint
                    result = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8")).get("description", "HTTP error")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    detail = "HTTP error"
                raise TelegramAPIError(f"Telegram API error {exc.code}: {detail}") from exc
            except (URLError, TimeoutError) as exc:
                raise TelegramAPIError("Unable to reach the Telegram API.") from exc

            if not result.get("ok"):
                raise TelegramAPIError(result.get("description", "Telegram API request failed."))
            return result["result"]

        return await asyncio.to_thread(request)

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._call("sendMessage", payload)

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = text
        await self._call("answerCallbackQuery", payload)

    async def remove_inline_keyboard(self, chat_id: int, message_id: int) -> None:
        await self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    async def send_typing(self, chat_id: int) -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram API returned an invalid bot profile.")
        return result

    async def set_commands(self) -> None:
        commands = [
            {"command": "start", "description": "Confirm that the bridge is online"},
            {"command": "help", "description": "Show available commands"},
            {"command": "status", "description": "Show Codex and Telegram status"},
            {"command": "projects", "description": "List available projects"},
            {"command": "server", "description": "Show local server resource usage"},
            {"command": "wiki", "description": "Ask or update the LLM wiki"},
            {"command": "car", "description": "Show vehicle status and controls"},
            {"command": "cancel", "description": "Cancel the active Codex request"},
        ]
        await self._call("setMyCommands", {"commands": commands})


class CodexRunner:
    def __init__(
        self,
        settings: Settings,
        profile: CodexProfile | None = None,
        workdir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._profile = profile or settings.default_profile
        self._workdir = (workdir or settings.codex_workdir).resolve()

    def command(self, output_path: str) -> list[str]:
        command = [
            self._settings.codex_bin,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--cd",
            str(self._workdir),
            "--output-last-message",
            output_path,
        ]
        if self._profile.model:
            command.extend(["--model", self._profile.model])
        if self._profile.reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{self._profile.reasoning_effort}"'])
        if self._settings.codex_unsafe_mode:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", "workspace-write", "--approve-for-me"])
        command.append("-")  # Feed the prompt through stdin so it does not appear in process listings.
        return command

    async def run(self, prompt: str) -> str:
        output_file = tempfile.NamedTemporaryFile(prefix="codex-telegram-", suffix=".txt", delete=False)
        output_path = output_file.name
        output_file.close()
        process: asyncio.subprocess.Process | None = None

        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(output_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workdir),
                start_new_session=True,
            )
            _, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")), timeout=self._profile.timeout_seconds
            )
            if process.returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(message[-1200:] or f"Codex exited with code {process.returncode}.")

            response = Path(output_path).read_text(encoding="utf-8").strip()
            if not response:
                raise RuntimeError("Codex completed without a final response.")
            return response
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Codex did not finish within {self._profile.timeout_seconds} seconds.") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"Could not find Codex executable: {self._settings.codex_bin}") from exc
        finally:
            if process and process.returncode is None:
                self._terminate_process_group(process.pid)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._kill_process_group(process.pid)
                    await process.wait()
            Path(output_path).unlink(missing_ok=True)

    @staticmethod
    def _terminate_process_group(pid: int) -> None:
        """End the owning Codex session and every child process it started."""

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill_process_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def version(self) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self._settings.codex_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (FileNotFoundError, asyncio.TimeoutError):
            return "unavailable"
        return stdout.decode("utf-8", errors="replace").strip() or "unavailable"


def split_reply(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return ["(No response)"]
    chunks: list[str] = []
    while len(text) > REPLY_CHUNK_SIZE:
        boundary = text.rfind("\n", 0, REPLY_CHUNK_SIZE)
        if boundary < REPLY_CHUNK_SIZE // 2:
            boundary = text.rfind(" ", 0, REPLY_CHUNK_SIZE)
        if boundary < REPLY_CHUNK_SIZE // 2:
            boundary = REPLY_CHUNK_SIZE
        chunks.append(text[:boundary].rstrip())
        text = text[boundary:].lstrip()
    chunks.append(text)
    return chunks


class SkodaGatewayClient:
    """Client for the JVM gateway that owns the Java client's TokenService."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.skoda_gateway_url
        self._token = settings.skoda_gateway_token
        self._timeout = settings.skoda_request_timeout_seconds

    async def snapshot(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/car")

    async def action(self, action: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/car/actions/{action}")

    async def _request(self, method: str, path: str) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            http_request = Request(
                self._url + path,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                method=method,
            )
            try:
                with urlopen(http_request, timeout=self._timeout) as response:  # noqa: S310 - configured gateway
                    raw = response.read()
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise VehicleApiError("authentication") from exc
                if exc.code in {408, 504}:
                    raise VehicleApiError("timeout") from exc
                if 500 <= exc.code < 600:
                    raise VehicleApiError("unavailable") from exc
                raise VehicleApiError("invalid") from exc
            except TimeoutError as exc:
                raise VehicleApiError("timeout") from exc
            except URLError as exc:
                reason = exc.reason
                kind: Literal["unavailable", "timeout"] = (
                    "timeout" if isinstance(reason, TimeoutError) else "unavailable"
                )
                raise VehicleApiError(kind) from exc
            except OSError as exc:
                raise VehicleApiError("unavailable") from exc
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise VehicleApiError("invalid") from exc
            if not isinstance(payload, dict):
                raise VehicleApiError("invalid")
            return payload

        return await asyncio.to_thread(request)


class Bridge:
    def __init__(self, settings: Settings, bot: TelegramBot) -> None:
        self._settings = settings
        self._bot = bot
        self._codex = CodexRunner(settings, settings.default_profile)
        self._fast_codex = CodexRunner(settings, settings.fast_profile)
        self._deep_codex = CodexRunner(settings, settings.deep_profile)
        self._wiki_codex = CodexRunner(settings, settings.default_profile, settings.wiki_workdir)
        self._car_api = SkodaGatewayClient(settings)
        self._jobs: dict[int, asyncio.Task[None]] = {}
        self._snapshot_cache: tuple[float, dict[str, Any]] | None = None
        self._action_last_started: dict[tuple[int, str], float] = {}

    def is_allowed(self, user_id: int) -> bool:
        return not self._settings.allowed_user_ids or user_id in self._settings.allowed_user_ids

    async def reply(
        self,
        chat_id: int,
        text: str,
        message_id: int | None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_reply(text)
        for index, chunk in enumerate(chunks):
            markup = reply_markup if index == len(chunks) - 1 else None
            await self._bot.send_message(chat_id, chunk, message_id, markup)

    async def require_car_access(self, chat_id: int, message_id: int | None) -> bool:
        if self._settings.allowed_user_ids:
            return True
        await self.reply(
            chat_id,
            "Vehicle details and controls are disabled until TELEGRAM_ALLOWED_USER_IDS is configured.",
            message_id,
        )
        return False

    async def handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        user_id = sender.get("id")
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if not isinstance(text, str) or not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if not self.is_allowed(user_id):
            await self.reply(chat_id, "Access denied.", message_id)
            return

        command, _, argument = text.strip().partition(" ")
        command = command.lower().split("@", 1)[0]

        if command == "/start":
            await self.reply(chat_id, "Codex bridge is online. Use /help for commands.", message_id)
        elif command == "/help":
            await self.reply(
                chat_id,
                "Commands:\n"
                "/status — Codex and Telegram status\n"
                "/projects — list folders in ~/Projects\n"
                "/server — server CPU, memory, temperature, and storage\n"
                "/wiki prompt — ask, explore, or update the LLM wiki\n"
                "/car — vehicle status, coordinates, and controls\n"
                "/quick prompt — fast Codex request\n"
                "/deep prompt — high-reasoning Codex request\n"
                "/cancel — stop the current Codex request\n\n"
                "Any ordinary text is also sent to Codex.",
                message_id,
            )
        elif command == "/status":
            await self.status(chat_id, message_id)
        elif command == "/projects":
            await self.projects(chat_id, message_id)
        elif command == "/server":
            await self.server(chat_id, message_id)
        elif command == "/wiki":
            await self.start_prompt(chat_id, message_id, argument, self._wiki_codex)
        elif command == "/car":
            if await self.require_car_access(chat_id, message_id):
                await self.start_car_snapshot(chat_id, message_id)
        elif command == "/cancel":
            await self.cancel(chat_id, message_id)
        elif command == "/quick":
            await self.start_prompt(chat_id, message_id, argument, self._fast_codex)
        elif command == "/deep":
            await self.start_prompt(chat_id, message_id, argument, self._deep_codex)
        elif command.startswith("/"):
            await self.reply(chat_id, "Unknown command. Use /help.", message_id)
        elif text.strip():
            await self.start_prompt(chat_id, message_id, text.strip(), self._codex)

    async def handle_callback_query(self, query: dict[str, Any]) -> None:
        callback_id = query.get("id")
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        data = query.get("data")
        if not all(
            (
                isinstance(callback_id, str),
                isinstance(user_id, int),
                isinstance(chat_id, int),
                isinstance(message_id, int),
                isinstance(data, str),
            )
        ):
            return
        if not self.is_allowed(user_id) or not self._settings.allowed_user_ids:
            await self._bot.answer_callback_query(callback_id, "Access denied.", show_alert=True)
            return
        if not data.startswith("car:"):
            await self._bot.answer_callback_query(callback_id)
            return

        active_job = self._jobs.get(chat_id)
        if data == "car:refresh":
            if active_job and not active_job.done():
                await self._bot.answer_callback_query(callback_id, "Another request is already running.", True)
                return
            await self._bot.answer_callback_query(callback_id, "Refreshing vehicle details…")
            await self.start_car_snapshot(chat_id, message_id)
            return

        if data == "car:cancel":
            await self._bot.answer_callback_query(callback_id, "Cancelled.")
            await self.remove_callback_keyboard(chat_id, message_id)
            return

        prefix = "car:confirm:"
        if data.startswith(prefix):
            action = data.removeprefix(prefix)
            if action not in CAR_ACTIONS:
                await self._bot.answer_callback_query(callback_id, "Unknown vehicle action.", True)
                return
            label, _ = CAR_ACTIONS[action]
            await self._bot.answer_callback_query(callback_id)
            await self.reply(
                chat_id,
                f"Confirm “{label}”? This will send a command to the vehicle.",
                message_id,
                car_confirmation_keyboard(action),
            )
            return

        prefix = "car:run:"
        if data.startswith(prefix):
            action = data.removeprefix(prefix)
            if action not in CAR_ACTIONS:
                await self._bot.answer_callback_query(callback_id, "Unknown vehicle action.", True)
                return
            if active_job and not active_job.done():
                await self._bot.answer_callback_query(callback_id, "Another request is already running.", True)
                return
            last_started = self._action_last_started.get((chat_id, action))
            if last_started is not None and time.monotonic() - last_started < self._settings.skoda_action_cooldown_seconds:
                await self._bot.answer_callback_query(callback_id, "That action was sent recently. Please wait.", True)
                return
            self._action_last_started[(chat_id, action)] = time.monotonic()
            label, _ = CAR_ACTIONS[action]
            await self._bot.answer_callback_query(callback_id, f"Sending: {label}…")
            await self.remove_callback_keyboard(chat_id, message_id)
            self.start_car_action(chat_id, message_id, action)
            return

        await self._bot.answer_callback_query(callback_id, "Unknown vehicle action.", True)

    async def remove_callback_keyboard(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.remove_inline_keyboard(chat_id, message_id)
        except TelegramAPIError:
            logging.warning("Could not remove a vehicle confirmation keyboard", exc_info=True)

    async def status(self, chat_id: int, message_id: int | None) -> None:
        access = "public (no allowlist)" if not self._settings.allowed_user_ids else "allowlist enabled"
        codex_version, telegram_profile = await asyncio.gather(
            self._codex.version(), self._telegram_profile(), return_exceptions=True
        )
        codex_status = (
            f"online ({codex_version})"
            if isinstance(codex_version, str) and codex_version != "unavailable"
            else "unavailable"
        )
        telegram_status = (
            self._format_telegram_status(telegram_profile)
            if isinstance(telegram_profile, dict)
            else "unavailable"
        )
        await self.reply(
            chat_id,
            "Bridge: online\n"
            f"Codex: {codex_status}\n"
            f"Telegram: {telegram_status}\n"
            f"Working directory: {self._settings.codex_workdir}\n"
            f"Projects directory: {self._settings.projects_dir}\n"
            f"Wiki directory: {self._settings.wiki_workdir}\n"
            f"Car gateway: {self._settings.skoda_gateway_url}\n"
            f"Unsafe mode: {'enabled' if self._settings.codex_unsafe_mode else 'disabled'}\n"
            f"Telegram access: {access}",
            message_id,
        )

    async def _telegram_profile(self) -> dict[str, Any]:
        return await self._bot.get_me()

    @staticmethod
    def _format_telegram_status(profile: dict[str, Any]) -> str:
        username = profile.get("username")
        if isinstance(username, str) and username:
            return f"online (@{username})"
        first_name = profile.get("first_name")
        if isinstance(first_name, str) and first_name:
            return f"online ({first_name})"
        return "online"

    async def projects(self, chat_id: int, message_id: int | None) -> None:
        projects_dir = self._settings.projects_dir
        try:
            projects = sorted(path.name for path in projects_dir.iterdir() if path.is_dir())
        except FileNotFoundError:
            await self.reply(chat_id, f"Projects directory is unavailable: {projects_dir}", message_id)
            return
        except OSError:
            logging.exception("Unable to list projects directory: %s", projects_dir)
            await self.reply(chat_id, "Unable to list the projects directory.", message_id)
            return
        if not projects:
            await self.reply(chat_id, f"No projects found in {projects_dir}.", message_id)
            return
        await self.reply(chat_id, "Projects:\n" + "\n".join(f"• {project}" for project in projects), message_id)

    async def server(self, chat_id: int, message_id: int | None) -> None:
        # CPU utilization requires a short sample interval; do it off the event loop.
        await self.reply(chat_id, await asyncio.to_thread(server_summary), message_id)

    async def start_car_snapshot(self, chat_id: int, message_id: int | None) -> None:
        active_job = self._jobs.get(chat_id)
        if active_job and not active_job.done():
            await self.reply(chat_id, "Another request is already running here. Use /cancel first.", message_id)
            return
        job = asyncio.create_task(self.run_car_snapshot(chat_id, message_id))
        self._jobs[chat_id] = job

    async def run_car_snapshot(self, chat_id: int, message_id: int | None) -> None:
        this_job = asyncio.current_task()
        started_at = time.monotonic()
        try:
            await self._bot.send_typing(chat_id)
            payload = await self.car_snapshot_payload()
            snapshot = CarSnapshot.from_payload(payload)
            await self.reply(
                chat_id,
                snapshot.format(),
                message_id,
                car_action_keyboard(),
            )
        except asyncio.CancelledError:
            await self.reply(chat_id, "Vehicle request cancelled.", message_id)
            raise
        except VehicleApiError as exc:
            logging.warning("Vehicle gateway snapshot failed: %s", exc.kind)
            messages = {
                "unavailable": "Vehicle service is currently unavailable.",
                "authentication": "Vehicle gateway authentication failed.",
                "timeout": "The vehicle did not respond in time.",
                "invalid": "Vehicle service returned an invalid response.",
                "configuration": "Vehicle API configuration is incomplete.",
            }
            await self.reply(chat_id, messages[exc.kind], message_id)
        except (RuntimeError, TelegramAPIError):
            logging.exception("Unable to retrieve vehicle details")
            await self.reply(chat_id, "Vehicle request failed.", message_id)
        finally:
            logging.info("car_snapshot_completed duration_ms=%d", (time.monotonic() - started_at) * 1000)
            if self._jobs.get(chat_id) is this_job:
                self._jobs.pop(chat_id, None)

    async def car_snapshot_payload(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._snapshot_cache and now - self._snapshot_cache[0] <= self._settings.skoda_snapshot_cache_seconds:
            return self._snapshot_cache[1]
        payload = await self._car_api.snapshot()
        self._snapshot_cache = (time.monotonic(), payload)
        return payload

    def start_car_action(self, chat_id: int, message_id: int, action: str) -> None:
        job = asyncio.create_task(self.run_car_action(chat_id, message_id, action))
        self._jobs[chat_id] = job

    async def run_car_action(self, chat_id: int, message_id: int, action: str) -> None:
        this_job = asyncio.current_task()
        label, endpoint = CAR_ACTIONS[action]
        try:
            await self._bot.send_typing(chat_id)
            await self._car_api.action(endpoint)
            await self.reply(chat_id, f"{label}: command accepted.", message_id)
        except asyncio.CancelledError:
            await self.reply(
                chat_id,
                f"{label} request cancelled locally; the vehicle may already have received the command.",
                message_id,
            )
            raise
        except VehicleApiError as exc:
            logging.warning("Vehicle gateway action failure action=%s kind=%s", action, exc.kind)
            if exc.kind == "timeout":
                await self.reply(chat_id, f"{label}: result is unknown; check the vehicle before retrying.", message_id)
            elif exc.kind == "authentication":
                await self.reply(chat_id, "Vehicle gateway authentication failed.", message_id)
            elif exc.kind == "configuration":
                await self.reply(chat_id, "Vehicle API configuration is incomplete.", message_id)
            else:
                await self.reply(chat_id, "Vehicle service is currently unavailable.", message_id)
        except (RuntimeError, TelegramAPIError):
            logging.exception("Unable to perform vehicle action %s", action)
            await self.reply(chat_id, f"{label} failed.", message_id)
        finally:
            if self._jobs.get(chat_id) is this_job:
                self._jobs.pop(chat_id, None)

    async def cancel(self, chat_id: int, message_id: int | None) -> None:
        job = self._jobs.get(chat_id)
        if not job or job.done():
            await self.reply(chat_id, "There is no active request.", message_id)
            return
        job.cancel()
        await self.reply(chat_id, "Cancelling the active request…", message_id)

    async def start_prompt(
        self, chat_id: int, message_id: int | None, prompt: str, runner: CodexRunner
    ) -> None:
        if not prompt:
            await self.reply(chat_id, "Send a prompt after the command.", message_id)
            return
        active_job = self._jobs.get(chat_id)
        if active_job and not active_job.done():
            await self.reply(chat_id, "Another request is already running here. Use /cancel first.", message_id)
            return
        job = asyncio.create_task(self.run_prompt(chat_id, message_id, prompt, runner))
        self._jobs[chat_id] = job

    async def run_prompt(self, chat_id: int, message_id: int | None, prompt: str, runner: CodexRunner) -> None:
        this_job = asyncio.current_task()
        try:
            await self._bot.send_typing(chat_id)
            response = await runner.run(prompt)
            await self.reply(chat_id, response, message_id)
        except asyncio.CancelledError:
            await self.reply(chat_id, "Codex request cancelled.", message_id)
            raise
        except (RuntimeError, TelegramAPIError) as exc:
            logging.exception("Unable to complete Codex request")
            await self.reply(chat_id, f"Codex request failed:\n{exc}", message_id)
        finally:
            if self._jobs.get(chat_id) is this_job:
                self._jobs.pop(chat_id, None)


async def run_bridge(settings: Settings) -> None:
    bot = TelegramBot(settings.telegram_token)
    bridge = Bridge(settings, bot)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(stop_signal, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await bot.set_commands()
    except TelegramAPIError as exc:
        logging.warning("Could not register Telegram command menu: %s", exc)

    logging.info("Telegram bridge started. Press Ctrl+C to stop.")
    offset: int | None = None
    while not stop_event.is_set():
        try:
            updates = await bot.get_updates(offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                message = update.get("message")
                if isinstance(message, dict):
                    await bridge.handle_message(message)
                callback_query = update.get("callback_query")
                if isinstance(callback_query, dict):
                    await bridge.handle_callback_query(callback_query)
        except TelegramAPIError as exc:
            logging.warning("Telegram polling failed: %s", exc)
            await asyncio.sleep(3)

    for job in bridge._jobs.values():
        job.cancel()
    await asyncio.gather(*bridge._jobs.values(), return_exceptions=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = Settings.load()
        asyncio.run(run_bridge(settings))
    except ConfigurationError as exc:
        logging.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
