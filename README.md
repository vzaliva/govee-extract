# govee-extract

Read CO₂, temperature and humidity from a **GoveeLife Smart Air Quality Monitor / CO2
Detector H5140** over Govee's cloud API. Single module, Python 3.9+, standard library only —
no runtime dependencies. Managed with [uv](https://docs.astral.sh/uv/).

## Which API this uses

The PDF at `govee-public.s3.amazonaws.com/developer-docs/GoveeDeveloperAPIReference.pdf`
documents the **legacy v1 API** (`developer-api.govee.com`). That API cannot see the
H5140 — its supported-model list covers lights, plugs, switches and appliances only.

Sensors live on Govee's newer **Platform API**, which is what this script targets:

| | |
|---|---|
| Device list | `GET https://openapi.api.govee.com/router/api/v1/user/devices` |
| Device state | `POST https://openapi.api.govee.com/router/api/v1/device/state` |
| Auth | `Govee-API-Key: <key>` header |

The H5140 reports as type `devices.types.air_quality_monitor`. Verified against real
hardware on 2026-08-01:

- The device list advertises `carbonDioxideConcentration`, `sensorTemperature` and
  `sensorHumidity`, **with no `parameters` block** — so no ranges and no declared units.
- The state response additionally returns `online`, which is *not* in the device list.
- Every state value is a bare scalar: `{"value": 620}`, not a nested object.

## Install

Run it without installing anything, straight from the repo:

```sh
uvx --from git+https://github.com/vzaliva/govee-extract govee-h5140 list
```

For repeated use — and for cron, where you want a stable path and no rebuild per run —
install it as a uv tool:

```sh
uv tool install git+https://github.com/vzaliva/govee-extract
govee-h5140 list                       # now on PATH at ~/.local/bin/govee-h5140
uv tool upgrade govee-extract          # later
```

Working on a checkout:

```sh
git clone git@github.com:vzaliva/govee-extract.git && cd govee-extract
uv run govee-h5140 list                # runs from source in a managed venv
uv tool install .                      # or install the local checkout
```

`uv` provisions the interpreter itself, so no system Python or virtualenv setup is needed.

## API key

Get a key in the Govee Home app: **Profile → About Us → Apply for API Key**. It arrives
by email. The script finds it from, in order:

1. `--api-key <key>`
2. `$GOVEE_API_KEY`
3. `~/.config/govee/api_key` (a file containing just the key)

```sh
mkdir -p ~/.config/govee && chmod 700 ~/.config/govee
printf '%s' 'YOUR-KEY-HERE' > ~/.config/govee/api_key
chmod 600 ~/.config/govee/api_key
```

## Usage

Examples below use the installed `govee-h5140` command. Substitute `uv run govee-h5140`
from a checkout, or `uvx --from git+https://github.com/vzaliva/govee-extract govee-h5140`
to run without installing.

```sh
govee-h5140 list          # every device the key can see, with capabilities
govee-h5140 read          # one reading from the H5140
```

```
Smart CO₂ Monitor  [H5140 12:BC:AC:27:6E:02:6C:7C]
  read at      2026-08-01 19:09:31 PDT
  CO2          620 ppm
  Temperature  20.7 °C
  Humidity     49.9 %
  Online       yes
```

Machine-readable, logging, and polling:

```sh
govee-h5140 read --json                    # one JSON object
govee-h5140 read --raw                     # unparsed API payload
govee-h5140 read --csv ~/co2.csv           # append a row
govee-h5140 read --watch 60 --csv ~/co2.csv  # poll every 60s forever
```

Selecting a device when you own more than one:

```sh
govee-h5140 read --device 12:BC:AC:27:6E:02:6C:7C
govee-h5140 read --name "study"            # substring, case-insensitive
govee-h5140 read --sku ''                  # match any model, not just H5140
```

### Temperature units

**The H5140 reports Fahrenheit and does not say so** — the API declares no unit anywhere.
This was confirmed against hardware (a room at 20.7 °C reported as `69.26`), so the script
assumes Fahrenheit for this SKU and outputs Celsius by default. No flag needed.

```sh
--temp-unit c|f|raw       # output unit; 'raw' passes the value through untouched
--assume-temp-unit c|f    # source unit, overriding what the script infers
```

Source unit is resolved in this order: `--assume-temp-unit`, then any unit the API
declares, then a per-SKU default from hardware testing (`SKU_TEMP_UNITS`). If none apply,
the value passes through unconverted with a warning rather than being guessed at.

One caveat worth knowing: it is not established whether Govee's Fahrenheit output is fixed
per model or follows the unit selected in the Govee app. If you switch the app to Celsius
and readings suddenly look 30-odd degrees too low, pass `--assume-temp-unit c`. The script
warns when it is about to treat an implausibly low value (below 50 °F) as Fahrenheit,
which is what that mistake looks like.

## Grafana / InfluxDB

Grafana never calls this script. It queries InfluxDB; this script writes to InfluxDB on
a timer. That's the same shape as `ecobee_influx_connector` and Powerwall-Dashboard's own
Telegraf collector, and it's why there's no web server or daemon here — cron is enough.

```
cron ──> govee-h5140 ──HTTP──> InfluxDB 1.8 <──query── Grafana
         (every 5 min)          (db: govee)             (dashboard)
```

### 1. Create the database

Powerwall-Dashboard runs `influxdb:1.8` on port 8086 with database `powerwall`. Give
Govee its own database so it stays clear of that stack's continuous queries, backups and
upgrades:

```sh
docker exec -it influxdb influx -execute 'CREATE DATABASE govee'
docker exec -it influxdb influx -execute 'SHOW DATABASES'
```

(Writing into `powerwall` instead works and saves adding a datasource — pass
`--influx-db powerwall`. It just mixes foreign data into an app-managed database.)

### 2. Write a reading

From the machine that will run the collector, with `<influx-host>` being the box running
Powerwall-Dashboard:

```sh
govee-h5140 read -q --influx-url http://<influx-host>:8086 --influx-db govee
```

Check it landed:

```sh
docker exec -it influxdb influx -database govee \
  -execute 'SELECT * FROM govee_air_quality ORDER BY time DESC LIMIT 5'
```

To see exactly what would be written without writing it, use `--line-protocol`:

```
govee_air_quality,sku=H5140,device=12:BC:AC:27:6E:02:6C:7C,name=Smart\ CO₂\ Monitor online=1i,co2_ppm=620.0,temperature_c=20.7,humidity_pct=49.9 1785636571802139136
```

### 3. Schedule it

```cron
*/5 * * * * $HOME/.local/bin/govee-h5140 read -q --influx-url http://<influx-host>:8086 --influx-db govee
```

Every 5 minutes is 288 API calls/day against a 10 000/day quota, so there's plenty of
headroom — every minute (1440/day) is fine too. The device list is cached for 24 h
(`--cache-ttl`), which keeps each poll to a single API call.

### 4. Add the datasource and dashboard

In Grafana: **Connections → Data sources → Add → InfluxDB**, query language *InfluxQL*,
URL `http://influxdb:8086` (the container name, since Grafana is in the same compose
network), database `govee`. Save & test.

Then **Dashboards → New → Import**, upload `grafana/govee-h5140-dashboard.json`, and pick
that datasource when prompted.

The dashboard has current-value stat tiles, a CO₂ time series with dashed threshold lines
at 800 / 1000 / 1400 ppm, and separate temperature and humidity panels. It's filtered by a
`device` variable, so adding a second Govee sensor later needs no edits.

### Fields written

| Field | Type | Notes |
|---|---|---|
| `co2_ppm` | float | |
| `temperature_c` | float | named `temperature_f` if you pass `--temp-unit f` |
| `humidity_pct` | float | |
| `online` | integer | 1/0 |

Tags: `sku`, `device`, `name`. Measurement: `govee_air_quality` (`--influx-measurement`).

Two deliberate choices here:

- **All sensor values are written as floats**, even CO₂, which always reads as a whole
  number. The device returns humidity as `49.9` but would return `50` as an integer, and
  InfluxDB rejects a point whose field type differs from the existing series — a stray
  integer would start silently failing writes with `field type conflict`.
- **The temperature field is named after its unit**, so switching `--temp-unit` starts a
  new series rather than mixing °C and °F into one.

### InfluxDB 2.x

```sh
govee-h5140 read -q --influx-url http://host:8086 \
  --influx-org myorg --influx-bucket govee --influx-token "$INFLUX_TOKEN"
```

`--influx-token` and `--influx-password` also read `$INFLUX_TOKEN` / `$INFLUX_PASSWORD`,
so credentials needn't appear in the crontab.

## Rate limits

Govee enforces 10 000 requests/account/day, plus per-endpoint limits (30/min for the
device list, 30/min/device for state). A `read` costs one request in the steady state —
the device list is cached for 24 h (`--cache-ttl`), so only the state call is made.
`--watch` warns below a 10 s interval, which is where the daily cap starts to bite.

429s and 5xxs are retried with exponential backoff, honouring `Retry-After` and
`RateLimit-Reset`. `-v` prints the rate-limit headers.

Exit codes: `0` success, `2` API/auth/selection error, `130` interrupted.

## cron example

```cron
*/5 * * * * $HOME/.local/bin/govee-h5140 read --csv $HOME/co2.csv >/dev/null
```

## Development

```sh
uv run test_govee_h5140.py            # 65 checks; no network, no API key
uv run --python 3.9 --no-project test_govee_h5140.py   # verify the requires-python floor
uv build                              # wheel + sdist into dist/
uv lock                               # refresh uv.lock
uvx --from . govee-h5140 --help       # run the packaged entry point from source
```

The suite stubs `GoveeClient._request` and `urllib.request.urlopen`, so it covers
capability decoding, unit conversion, CSV/JSON/line-protocol output, the device cache,
device selection and error paths without touching the network. It is a flat script of
`check(label, condition)` calls rather than pytest, so run the whole file.

Tested on CPython 3.9 through 3.13. `uv.lock` is committed because this is an application
rather than a library; there are no runtime dependencies to resolve.

## Notes

- Only Wi-Fi devices appear on this API; a Bluetooth-only H5140 will not be listed.
- Unrecognised capability instances are still reported under their raw instance name, so
  the script keeps working as Govee adds sensors. Use `--raw` to see exactly what your
  unit returns.
- Values come from Govee's cloud cache, so they lag the display by up to a minute or two.
