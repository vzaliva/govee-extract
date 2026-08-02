#!/usr/bin/env python3
"""Read sensor values from a GoveeLife Smart Air Quality Monitor / CO2 Detector (H5140).

Uses the Govee *Platform* API (https://openapi.api.govee.com/router/api/v1), which is
the API that exposes sensor devices.  The older v1 API documented in
GoveeDeveloperAPIReference.pdf (developer-api.govee.com) only covers lights, plugs,
switches and appliances -- it does not list the H5140 at all.

Get an API key from the Govee Home app: Profile -> About Us -> Apply for API Key.

Only uses the Python standard library.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

API_ROOT = "https://openapi.api.govee.com/router/api/v1"
DEVICES_URL = f"{API_ROOT}/user/devices"
STATE_URL = f"{API_ROOT}/device/state"

DEFAULT_SKU = "H5140"

# The Govee daily quota is 10000 requests/account/day, so anything faster than
# ~8.7s between polls will exhaust it before the day is out.
MIN_SAFE_INTERVAL = 10.0


class GoveeError(Exception):
    """Any failure talking to the Govee API."""


class GoveeAuthError(GoveeError):
    """The API key was rejected."""


class GoveeRateLimitError(GoveeError):
    """Rate limited and out of retries."""


# --------------------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------------------


class GoveeClient:
    """Minimal client for the two Platform API endpoints we need."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 4,
        verbose: bool = False,
    ) -> None:
        if not api_key:
            raise GoveeAuthError("no API key supplied")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose
        self._ssl_ctx = ssl.create_default_context()
        self.last_rate_limit: dict[str, str] = {}

    # -- low level ---------------------------------------------------------------------

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {
            "Govee-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "govee-h5140/1.0 (+stdlib urllib)",
        }

        delay = 2.0
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                    self._note_rate_limit(resp.headers)
                    raw = resp.read().decode("utf-8", "replace")
                    return self._decode(raw, resp.status)
            except urllib.error.HTTPError as exc:
                self._note_rate_limit(exc.headers)
                raw = exc.read().decode("utf-8", "replace")
                if exc.code in (401, 403):
                    raise GoveeAuthError(
                        f"HTTP {exc.code}: API key rejected. {_brief(raw)}"
                    ) from exc
                if exc.code == 429:
                    last_exc = GoveeRateLimitError(f"HTTP 429: rate limited. {_brief(raw)}")
                    wait = self._retry_after(exc.headers, delay)
                    if attempt == self.max_retries:
                        break
                    self._log(f"rate limited, sleeping {wait:.1f}s (attempt {attempt}/{self.max_retries})")
                    time.sleep(wait)
                    delay *= 2
                    continue
                if 500 <= exc.code < 600:
                    last_exc = GoveeError(f"HTTP {exc.code}: server error. {_brief(raw)}")
                    if attempt == self.max_retries:
                        break
                    self._log(f"server error {exc.code}, retrying in {delay:.1f}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GoveeError(f"HTTP {exc.code}: {_brief(raw)}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = GoveeError(f"network error: {exc}")
                if attempt == self.max_retries:
                    break
                self._log(f"network error ({exc}), retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 2

        raise last_exc if last_exc else GoveeError("request failed for an unknown reason")

    @staticmethod
    def _decode(raw: str, http_status: int) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoveeError(f"non-JSON response (HTTP {http_status}): {_brief(raw)}") from exc
        if not isinstance(data, dict):
            raise GoveeError(f"unexpected JSON response: {_brief(raw)}")
        # The API reports its own status code in the body, which can disagree with HTTP.
        code = data.get("code")
        if code is not None and int(code) != 200:
            msg = data.get("msg") or data.get("message") or "unknown error"
            if int(code) in (401, 403):
                raise GoveeAuthError(f"API error {code}: {msg}")
            if int(code) == 429:
                raise GoveeRateLimitError(f"API error {code}: {msg}")
            raise GoveeError(f"API error {code}: {msg}")
        return data

    def _note_rate_limit(self, headers: Any) -> None:
        found = {}
        for key, value in (headers.items() if headers else []):
            if "ratelimit" in key.lower():
                found[key] = value
        if found:
            self.last_rate_limit = found
            self._log("rate limit: " + ", ".join(f"{k}={v}" for k, v in sorted(found.items())))

    @staticmethod
    def _retry_after(headers: Any, fallback: float) -> float:
        for key in ("Retry-After", "API-RateLimit-Reset", "X-RateLimit-Reset", "RateLimit-Reset"):
            value = headers.get(key) if headers else None
            if not value:
                continue
            try:
                num = float(value)
            except ValueError:
                continue
            # Reset headers are absolute UTC epoch seconds; Retry-After is relative.
            wait = num - time.time() if num > 1_000_000_000 else num
            if 0 < wait <= 300:
                return wait
        return fallback

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[govee] {message}", file=sys.stderr)

    # -- endpoints ---------------------------------------------------------------------

    def devices(self) -> list[dict[str, Any]]:
        """GET /user/devices -- every device on the account the API can see."""
        data = self._request("GET", DEVICES_URL)
        payload = data.get("data")
        if payload is None:
            payload = data.get("payload")
        if isinstance(payload, dict):  # some responses wrap the list
            payload = payload.get("devices", [])
        return list(payload or [])

    def device_state(self, sku: str, device: str) -> dict[str, Any]:
        """POST /device/state -- current capability values for one device."""
        body = {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": sku, "device": device},
        }
        data = self._request("POST", STATE_URL, body)
        return data.get("payload") or {}


def _brief(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


# --------------------------------------------------------------------------------------
# Capability decoding
# --------------------------------------------------------------------------------------

# instance name -> (output field, display label, unit).  Anything not listed here is
# still reported, just under its raw instance name -- Govee adds instances over time.
KNOWN_INSTANCES: dict[str, tuple[str, str, str | None]] = {
    "carbonDioxideConcentration": ("co2_ppm", "CO2", "ppm"),
    "sensorTemperature": ("temperature", "Temperature", None),  # unit is discovered
    "sensorHumidity": ("humidity_pct", "Humidity", "%"),
    "pm25": ("pm25", "PM2.5", "ug/m3"),
    "online": ("online", "Online", None),
    "powerSwitch": ("power", "Power", None),
    "batteryLevel": ("battery_pct", "Battery", "%"),
}

# Keys Govee nests real values behind, depending on capability.
_NESTED_VALUE_KEYS = (
    "value",
    "currentTemperature",
    "currentHumidity",
    "currentValue",
    "carbonDioxide",
)


@dataclass
class Reading:
    """One decoded snapshot of a device."""

    sku: str
    device: str
    name: str
    taken_at: datetime
    values: dict[str, Any] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _unwrap(value: Any) -> tuple[Any, str | None]:
    """Reduce a capability state to (scalar, unit) where possible."""
    unit = None
    seen = 0
    while isinstance(value, dict) and seen < 4:
        seen += 1
        unit = value.get("unit") or unit
        for key in _NESTED_VALUE_KEYS:
            if key in value:
                value = value[key]
                break
        else:
            # A dict we don't recognise -- hand it back whole rather than guessing.
            return value, unit
    return value, unit


def decode_capabilities(caps: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Turn the capabilities array into flat {field: value}, {field: unit}, {field: label}."""
    values: dict[str, Any] = {}
    units: dict[str, str] = {}
    labels: dict[str, str] = {}

    for cap in caps or []:
        instance = cap.get("instance")
        if not instance:
            continue
        state = cap.get("state")
        if state is None:
            continue
        value, unit = _unwrap(state)
        if isinstance(value, dict) and not value:
            continue

        field_name, label, default_unit = KNOWN_INSTANCES.get(instance, (instance, instance, None))
        values[field_name] = value
        labels[field_name] = label
        chosen = unit or default_unit
        if chosen:
            units[field_name] = chosen

    return values, units, labels


def parse_state(payload: dict[str, Any], *, name: str = "", taken_at: datetime | None = None) -> Reading:
    values, units, labels = decode_capabilities(payload.get("capabilities", []))
    return Reading(
        sku=payload.get("sku", ""),
        device=payload.get("device", ""),
        name=name,
        taken_at=taken_at or datetime.now(timezone.utc),
        values=values,
        units=units,
        labels=labels,
        raw=payload,
    )


# --------------------------------------------------------------------------------------
# Temperature units
# --------------------------------------------------------------------------------------


def _normalise_unit(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    if t in ("c", "celsius", "centigrade", "°c", "degc"):
        return "C"
    if t in ("f", "fahrenheit", "°f", "degf"):
        return "F"
    return None


def temperature_unit_from_device(device_entry: dict[str, Any]) -> str | None:
    """Look for a declared temperature unit in a /user/devices capability definition."""
    for cap in device_entry.get("capabilities", []) or []:
        if cap.get("instance") != "sensorTemperature":
            continue
        params = cap.get("parameters") or {}
        unit = _normalise_unit(params.get("unit"))
        if unit:
            return unit
        rng = params.get("range") or {}
        unit = _normalise_unit(rng.get("unit"))
        if unit:
            return unit
    return None


def convert_temperature(value: float, source: str, target: str) -> float:
    if source == target:
        return value
    if source == "F" and target == "C":
        return (value - 32.0) * 5.0 / 9.0
    if source == "C" and target == "F":
        return value * 9.0 / 5.0 + 32.0
    raise ValueError(f"cannot convert {source} -> {target}")


def apply_temperature_unit(reading: Reading, source: str | None, target: str) -> Reading:
    """Convert reading['temperature'] into `target` if we know what unit it arrived in."""
    value = reading.values.get("temperature")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return reading

    source = source or _normalise_unit(reading.units.get("temperature"))
    if target == "raw" or source is None:
        if source is None and target != "raw":
            print(
                "[govee] warning: the API did not declare a temperature unit; reporting the raw "
                "value. Pass --assume-temp-unit f (or c) to convert.",
                file=sys.stderr,
            )
        reading.units.setdefault("temperature", source or "unknown")
        return reading

    reading.values["temperature"] = round(convert_temperature(float(value), source, target), 2)
    reading.units["temperature"] = target
    return reading


# --------------------------------------------------------------------------------------
# API key resolution
# --------------------------------------------------------------------------------------

KEY_FILE = Path.home() / ".config" / "govee" / "api_key"


def resolve_api_key(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("GOVEE_API_KEY")
    if env and env.strip():
        return env.strip()
    if KEY_FILE.is_file():
        text = KEY_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise GoveeAuthError(
        "No API key. Pass --api-key, set GOVEE_API_KEY, or write the key to "
        f"{KEY_FILE}.\nRequest a key in the Govee Home app: Profile -> About Us -> "
        "Apply for API Key."
    )


# --------------------------------------------------------------------------------------
# Device selection
# --------------------------------------------------------------------------------------


def select_device(
    devices: list[dict[str, Any]], *, sku: str | None, device_id: str | None, name: str | None
) -> dict[str, Any]:
    candidates = devices
    if sku:
        candidates = [d for d in candidates if str(d.get("sku", "")).upper() == sku.upper()]
    if device_id:
        wanted = device_id.strip().upper()
        candidates = [d for d in candidates if str(d.get("device", "")).upper() == wanted]
    if name:
        wanted = name.strip().lower()
        candidates = [d for d in candidates if wanted in str(d.get("deviceName", "")).lower()]

    if not candidates:
        known = ", ".join(
            f"{d.get('sku')}/{d.get('deviceName')}" for d in devices
        ) or "none"
        raise GoveeError(
            f"No device matched (sku={sku!r}, device={device_id!r}, name={name!r}). "
            f"Devices on this account: {known}. Run `list` to see them all."
        )
    if len(candidates) > 1:
        listing = "\n".join(
            f"  {d.get('sku')}  {d.get('device')}  {d.get('deviceName')}" for d in candidates
        )
        raise GoveeError(
            f"{len(candidates)} devices matched; narrow it with --device or --name:\n{listing}"
        )
    return candidates[0]


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

# Fixed leading columns keep the CSV stable as Govee adds capabilities.
CSV_FIELDS = [
    "timestamp",
    "sku",
    "device",
    "name",
    "co2_ppm",
    "temperature",
    "temperature_unit",
    "humidity_pct",
    "online",
]

_DISPLAY_ORDER = ["co2_ppm", "temperature", "humidity_pct", "pm25", "battery_pct", "online"]


def format_human(reading: Reading) -> str:
    header = f"{reading.name or '(unnamed)'}  [{reading.sku} {reading.device}]"
    stamp = reading.taken_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [header, f"  read at      {stamp}"]

    ordered = [k for k in _DISPLAY_ORDER if k in reading.values]
    ordered += [k for k in reading.values if k not in ordered]

    for key in ordered:
        value = reading.values[key]
        label = reading.labels.get(key, key)
        unit = reading.units.get(key, "")
        if isinstance(value, bool):
            shown = "yes" if value else "no"
        elif isinstance(value, dict):
            shown = json.dumps(value, separators=(",", ":"))
        elif unit in ("C", "F"):
            shown = f"{value} °{unit}"
        elif unit:
            shown = f"{value} {unit}"
        else:
            shown = str(value)
        lines.append(f"  {label:<12} {shown}")
    return "\n".join(lines)


def reading_to_dict(reading: Reading) -> dict[str, Any]:
    out: dict[str, Any] = {
        "timestamp": reading.taken_at.isoformat(),
        "sku": reading.sku,
        "device": reading.device,
        "name": reading.name,
    }
    out.update(reading.values)
    if reading.units:
        out["units"] = reading.units
    return out


def csv_row(reading: Reading) -> dict[str, Any]:
    row = {
        "timestamp": reading.taken_at.isoformat(),
        "sku": reading.sku,
        "device": reading.device,
        "name": reading.name,
        "temperature_unit": reading.units.get("temperature", ""),
    }
    for key in ("co2_ppm", "temperature", "humidity_pct", "online"):
        value = reading.values.get(key)
        if value is None:
            row[key] = ""
        elif isinstance(value, bool):  # avoid Python's "True"/"False" in a CSV
            row[key] = "true" if value else "false"
        else:
            row[key] = value
    return row


def append_csv(path: Path, reading: Reading) -> None:
    new_file = not path.exists() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(csv_row(reading))
        fh.flush()


# --------------------------------------------------------------------------------------
# InfluxDB line protocol
# --------------------------------------------------------------------------------------

DEFAULT_MEASUREMENT = "govee_air_quality"


def _escape_key(text: str) -> str:
    """Escape a measurement name, tag key or tag value for line protocol."""
    return str(text).replace("\\", "\\\\").replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


def to_line_protocol(reading: Reading, measurement: str = DEFAULT_MEASUREMENT) -> str:
    """Render a reading as one InfluxDB line-protocol point (nanosecond precision)."""
    tags = {"sku": reading.sku, "device": reading.device}
    if reading.name:
        tags["name"] = reading.name
    tag_str = ",".join(f"{_escape_key(k)}={_escape_key(v)}" for k, v in tags.items() if v)

    fields: list[str] = []
    for key, value in reading.values.items():
        if isinstance(value, bool):
            fields.append(f"{_escape_key(key)}={1 if value else 0}i")
        elif isinstance(value, int):
            fields.append(f"{_escape_key(key)}={value}i")
        elif isinstance(value, float):
            fields.append(f"{_escape_key(key)}={value}")
        # dicts and strings are skipped: not useful as Influx numeric fields

    if not fields:
        raise GoveeError("no numeric values in this reading; nothing to write to InfluxDB")

    # Rename the temperature field after the unit it is actually in, so a dashboard can
    # never silently mix Celsius and Fahrenheit in one series.
    unit = reading.units.get("temperature")
    if unit in ("C", "F") and "temperature=" in ",".join(fields):
        suffix = "_c" if unit == "C" else "_f"
        fields = [f.replace("temperature=", f"temperature{suffix}=", 1) if f.startswith("temperature=") else f
                  for f in fields]

    ts = int(reading.taken_at.timestamp() * 1_000_000_000)
    prefix = _escape_key(measurement) + (f",{tag_str}" if tag_str else "")
    return f"{prefix} {','.join(fields)} {ts}"


class InfluxWriter:
    """Writes line protocol to InfluxDB 1.x (/write) or 2.x (/api/v2/write)."""

    def __init__(
        self,
        url: str,
        *,
        db: str | None = None,
        user: str | None = None,
        password: str | None = None,
        org: str | None = None,
        bucket: str | None = None,
        token: str | None = None,
        timeout: float = 15.0,
        verbose: bool = False,
    ) -> None:
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self.headers = {"Content-Type": "text/plain; charset=utf-8"}

        if token:  # InfluxDB 2.x
            if not (org and bucket):
                raise GoveeError("--influx-token also needs --influx-org and --influx-bucket")
            query = urllib.parse.urlencode({"org": org, "bucket": bucket, "precision": "ns"})
            self.endpoint = f"{self.base}/api/v2/write?{query}"
            self.headers["Authorization"] = f"Token {token}"
        else:  # InfluxDB 1.x
            if not db:
                raise GoveeError("--influx-db is required for InfluxDB 1.x")
            self.endpoint = f"{self.base}/write?{urllib.parse.urlencode({'db': db, 'precision': 'ns'})}"
            if user:
                creds = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
                self.headers["Authorization"] = f"Basic {creds}"

    def write(self, line: str) -> None:
        req = urllib.request.Request(
            self.endpoint, data=line.encode("utf-8"), headers=self.headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status not in (200, 204):
                    raise GoveeError(f"InfluxDB returned HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            body = _brief(exc.read().decode("utf-8", "replace"))
            hint = ""
            if exc.code == 404:
                hint = " (does the database exist? CREATE DATABASE on InfluxDB 1.x)"
            elif exc.code in (401, 403):
                hint = " (check --influx-user/--influx-password or --influx-token)"
            raise GoveeError(f"InfluxDB write failed: HTTP {exc.code}{hint}. {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GoveeError(f"InfluxDB unreachable at {self.base}: {exc}") from exc
        if self.verbose:
            print(f"[govee] wrote 1 point to {self.endpoint}", file=sys.stderr)


# --------------------------------------------------------------------------------------
# Device list cache
# --------------------------------------------------------------------------------------

CACHE_FILE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "govee" / "devices.json"


def load_devices(client: GoveeClient, *, ttl: float, verbose: bool = False) -> list[dict[str, Any]]:
    """Device list, cached on disk.

    A cron-driven read otherwise costs two API calls (list + state) where one will do.
    The list only changes when you add or rename a device, so caching it for a day keeps
    steady-state usage at one call per poll.
    """
    if ttl > 0 and CACHE_FILE.is_file():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < ttl:
            try:
                cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(cached, list) and cached:
                    if verbose:
                        print(f"[govee] device list from cache ({age:.0f}s old)", file=sys.stderr)
                    return cached
            except (json.JSONDecodeError, OSError):
                pass  # corrupt cache is not worth failing over

    devices = client.devices()
    if ttl > 0 and devices:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(devices), encoding="utf-8")
        except OSError as exc:
            if verbose:
                print(f"[govee] could not write cache: {exc}", file=sys.stderr)
    return devices


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace, client: GoveeClient) -> int:
    devices = client.devices()
    if args.json or args.raw:
        print(json.dumps(devices, indent=2))
        return 0
    if not devices:
        print("No devices visible to this API key.")
        print("Note: the Platform API only exposes Wi-Fi devices, and only after the")
        print("device has been added to the Govee Home account the key belongs to.")
        return 1
    for dev in devices:
        instances = [c.get("instance") for c in dev.get("capabilities", []) or [] if c.get("instance")]
        print(f"{dev.get('sku', '?'):<8} {dev.get('device', '?')}  {dev.get('deviceName', '')}")
        print(f"         type: {dev.get('type', 'unknown')}")
        if instances:
            print(f"         capabilities: {', '.join(instances)}")
    return 0


def _fetch(client: GoveeClient, entry: dict[str, Any], args: argparse.Namespace) -> Reading:
    payload = client.device_state(entry["sku"], entry["device"])
    reading = parse_state(payload, name=entry.get("deviceName", ""))
    source = _normalise_unit(args.assume_temp_unit) or temperature_unit_from_device(entry)
    return apply_temperature_unit(reading, source, args.temp_unit)


def build_influx_writer(args: argparse.Namespace) -> InfluxWriter | None:
    if not args.influx_url:
        return None
    return InfluxWriter(
        args.influx_url,
        db=args.influx_db,
        user=args.influx_user,
        password=args.influx_password or os.environ.get("INFLUX_PASSWORD"),
        org=args.influx_org,
        bucket=args.influx_bucket,
        token=args.influx_token or os.environ.get("INFLUX_TOKEN"),
        timeout=args.timeout,
        verbose=args.verbose,
    )


def cmd_read(args: argparse.Namespace, client: GoveeClient) -> int:
    influx = build_influx_writer(args)
    devices = load_devices(client, ttl=args.cache_ttl, verbose=args.verbose)
    try:
        entry = select_device(devices, sku=args.sku, device_id=args.device, name=args.name)
    except GoveeError:
        if args.cache_ttl <= 0:
            raise
        # A renamed or newly added device makes the cache wrong rather than stale.
        if args.verbose:
            print("[govee] no match in cached device list, refreshing", file=sys.stderr)
        devices = load_devices(client, ttl=0, verbose=args.verbose)
        entry = select_device(devices, sku=args.sku, device_id=args.device, name=args.name)

    interval = args.watch
    if interval is not None and interval < MIN_SAFE_INTERVAL:
        print(
            f"[govee] warning: --watch {interval}s exceeds the 10000 requests/day account "
            f"quota; {MIN_SAFE_INTERVAL:.0f}s or slower is safe.",
            file=sys.stderr,
        )

    csv_path = Path(args.csv).expanduser() if args.csv else None
    first = True

    while True:
        if not first:
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                return 0
        first = False

        try:
            reading = _fetch(client, entry, args)
        except GoveeError as exc:
            if interval is None:
                raise
            print(f"[govee] read failed: {exc}", file=sys.stderr)
            continue

        if args.raw:
            print(json.dumps(reading.raw, indent=2), flush=True)
        elif args.line_protocol:
            print(to_line_protocol(reading, args.influx_measurement), flush=True)
        elif args.json:
            print(json.dumps(reading_to_dict(reading), separators=(",", ": ")), flush=True)
        elif not args.quiet:
            print(format_human(reading), flush=True)

        if csv_path:
            append_csv(csv_path, reading)

        if influx:
            try:
                influx.write(to_line_protocol(reading, args.influx_measurement))
            except GoveeError as exc:
                if interval is None:
                    raise
                print(f"[govee] influx write failed: {exc}", file=sys.stderr)

        if interval is None:
            return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        # prog is left to argparse so the installed entry point reports itself as
        # "govee-h5140" while direct execution reports "govee_h5140.py".
        description="Read CO2, temperature and humidity from a Govee H5140 air quality monitor.",
        epilog=(
            "API key: --api-key, $GOVEE_API_KEY, or ~/.config/govee/api_key. "
            "Request one in the Govee Home app under Profile -> About Us -> Apply for API Key."
        ),
    )
    parser.add_argument("--api-key", help="Govee Developer API key")
    parser.add_argument("-v", "--verbose", action="store_true", help="log retries and rate-limit headers")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=4, help="attempts per request (default 4)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list every device this API key can see")
    p_list.add_argument("--json", action="store_true", help="print the device list as JSON")
    p_list.add_argument("--raw", action="store_true", help="alias for --json")
    p_list.set_defaults(func=cmd_list)

    p_read = sub.add_parser("read", help="read the current sensor values")
    p_read.add_argument("--sku", default=DEFAULT_SKU, help=f"model to match (default {DEFAULT_SKU}; use '' for any)")
    p_read.add_argument("--device", help="device id / MAC, if you own more than one")
    p_read.add_argument("--name", help="match on device name (substring, case-insensitive)")
    p_read.add_argument("--json", action="store_true", help="one JSON object per reading")
    p_read.add_argument("--raw", action="store_true", help="dump the unparsed API payload")
    p_read.add_argument("--csv", metavar="PATH", help="append each reading to a CSV file")
    p_read.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="poll forever at this interval instead of reading once",
    )
    p_read.add_argument(
        "--temp-unit",
        choices=("c", "f", "raw"),
        default="c",
        help="temperature unit to report (default c; 'raw' leaves it untouched)",
    )
    p_read.add_argument(
        "--assume-temp-unit",
        choices=("c", "f"),
        help="unit the API reports in, when it doesn't say so itself",
    )
    p_read.add_argument("-q", "--quiet", action="store_true", help="suppress stdout (for cron)")
    p_read.add_argument(
        "--cache-ttl",
        type=float,
        default=86400.0,
        metavar="SECONDS",
        help="cache the device list this long, halving API calls (default 86400; 0 disables)",
    )

    influx = p_read.add_argument_group(
        "InfluxDB output",
        "Write each reading to InfluxDB. 1.x needs --influx-db; 2.x needs --influx-token, "
        "--influx-org and --influx-bucket.",
    )
    influx.add_argument("--influx-url", metavar="URL", help="e.g. http://10.0.0.5:8086")
    influx.add_argument("--influx-db", default="govee", help="InfluxDB 1.x database (default govee)")
    influx.add_argument("--influx-user", help="InfluxDB 1.x username, if auth is enabled")
    influx.add_argument("--influx-password", help="InfluxDB 1.x password (or $INFLUX_PASSWORD)")
    influx.add_argument("--influx-org", help="InfluxDB 2.x org")
    influx.add_argument("--influx-bucket", help="InfluxDB 2.x bucket")
    influx.add_argument("--influx-token", help="InfluxDB 2.x token (or $INFLUX_TOKEN)")
    influx.add_argument(
        "--influx-measurement",
        default=DEFAULT_MEASUREMENT,
        help=f"measurement name (default {DEFAULT_MEASUREMENT})",
    )
    influx.add_argument(
        "--line-protocol",
        action="store_true",
        help="print line protocol to stdout instead of writing (for Telegraf exec, or testing)",
    )
    p_read.set_defaults(func=cmd_read)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if getattr(args, "temp_unit", None) in ("c", "f"):
        args.temp_unit = args.temp_unit.upper()
    if getattr(args, "sku", None) == "":
        args.sku = None

    try:
        client = GoveeClient(
            resolve_api_key(args.api_key),
            timeout=args.timeout,
            max_retries=max(1, args.retries),
            verbose=args.verbose,
        )
        return args.func(args, client)
    except GoveeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
