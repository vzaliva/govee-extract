"""Offline tests for govee_h5140: stubs the HTTP layer and checks parsing,
unit handling, output formats and error paths. Run with: python3 test_govee_h5140.py"""
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import govee_h5140 as g

DEVICES = [
    {
        "sku": "H5140",
        "device": "DE:76:17:29:AA:BB:CC:DD",
        "deviceName": "Study CO2",
        "type": "devices.types.air_quality_monitor",
        "capabilities": [
            {"type": "devices.capabilities.online", "instance": "online",
             "parameters": {"dataType": "ENUM", "options": [{"name": "online", "value": True}]}},
            {"type": "devices.capabilities.property", "instance": "sensorTemperature",
             "parameters": {"dataType": "INTEGER", "unit": "Fahrenheit",
                            "range": {"min": -20, "max": 140, "precision": 1}}},
            {"type": "devices.capabilities.property", "instance": "sensorHumidity",
             "parameters": {"dataType": "INTEGER", "unit": "percent",
                            "range": {"min": 0, "max": 100, "precision": 1}}},
            {"type": "devices.capabilities.property", "instance": "carbonDioxideConcentration",
             "parameters": {"dataType": "INTEGER", "unit": "ppm",
                            "range": {"min": 400, "max": 9999, "precision": 1}}},
        ],
    },
    {"sku": "H6159", "device": "99:E5:A4:C1:38:29:DA:7B", "deviceName": "Lamp",
     "type": "devices.types.light", "capabilities": []},
]

STATE = {
    "requestId": "x", "msg": "success", "code": 200,
    "payload": {
        "sku": "H5140",
        "device": "DE:76:17:29:AA:BB:CC:DD",
        "capabilities": [
            {"type": "devices.capabilities.online", "instance": "online", "state": {"value": True}},
            {"type": "devices.capabilities.property", "instance": "sensorTemperature",
             "state": {"value": 70.52}},
            # deliberately nested differently, to check the unwrapper
            {"type": "devices.capabilities.property", "instance": "sensorHumidity",
             "state": {"value": {"currentHumidity": 47}}},
            {"type": "devices.capabilities.property", "instance": "carbonDioxideConcentration",
             "state": {"value": 812}},
            # an instance the script has never heard of
            {"type": "devices.capabilities.property", "instance": "someFutureSensor",
             "state": {"value": 3.5, "unit": "widgets"}},
        ],
    },
}


def fake_request(self, method, url, body=None):
    if url == g.DEVICES_URL:
        return {"code": 200, "message": "success", "data": DEVICES}
    if url == g.STATE_URL:
        assert "requestId" in body
        if body["payload"]["sku"] != "H5140":  # e.g. the lamp: no sensor capabilities
            return {"code": 200, "msg": "success",
                    "payload": {"sku": body["payload"]["sku"],
                                "device": body["payload"]["device"], "capabilities": []}}
        return STATE
    raise AssertionError(url)


g.GoveeClient._request = fake_request

# Keep the device-list cache out of the real ~/.cache during tests.
g.CACHE_FILE = Path(tempfile.mkdtemp()) / "devices.json"
KEEP_CACHE = False

FAILS = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def run(argv):
    if not KEEP_CACHE:
        g.CACHE_FILE.unlink(missing_ok=True)
    buf, err = io.StringIO(), io.StringIO()
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err
    try:
        rc = g.main(argv)
    finally:
        sys.stdout, sys.stderr = so, se
    return rc, buf.getvalue(), err.getvalue()


print("=== list ===")
rc, out, err = run(["--api-key", "k", "list"])
print(out.rstrip())
check("list exits 0", rc == 0)
check("list shows H5140", "H5140" in out and "Study CO2" in out)
check("list shows capabilities", "carbonDioxideConcentration" in out)

print("\n=== read (human, default C) ===")
rc, out, err = run(["--api-key", "k", "read"])
print(out.rstrip())
if err.strip():
    print("stderr:", err.strip())
check("read exits 0", rc == 0)
check("CO2 reported", "812 ppm" in out, out)
check("humidity unwrapped from nested dict", "47 %" in out, out)
check("70.52F converted to 21.4C", "21.4 °C" in out, out)
check("unknown instance still shown", "someFutureSensor" in out and "3.5 widgets" in out, out)
check("online rendered as yes", "yes" in out, out)
check("no unit warning when API declares one", "warning" not in err, err)

print("\n=== read --json --temp-unit f ===")
rc, out, err = run(["--api-key", "k", "read", "--json", "--temp-unit", "f"])
print(out.rstrip())
obj = json.loads(out)
check("json co2", obj["co2_ppm"] == 812)
check("json temp stays F", obj["temperature"] == 70.52, str(obj["temperature"]))
check("json humidity", obj["humidity_pct"] == 47)
check("json units block", obj["units"]["temperature"] == "F")

print("\n=== read --temp-unit raw ===")
rc, out, err = run(["--api-key", "k", "read", "--json", "--temp-unit", "raw"])
check("raw leaves value alone", json.loads(out)["temperature"] == 70.52)

print("\n=== unit not declared by API -> warns, does not convert ===")
saved = DEVICES[0]["capabilities"][1]["parameters"].pop("unit")
rc, out, err = run(["--api-key", "k", "read", "--json"])
check("warns about unknown unit", "warning" in err, err)
check("value untouched when unit unknown", json.loads(out)["temperature"] == 70.52)
rc, out, err = run(["--api-key", "k", "read", "--json", "--assume-temp-unit", "f"])
check("--assume-temp-unit converts", json.loads(out)["temperature"] == 21.4, out)
DEVICES[0]["capabilities"][1]["parameters"]["unit"] = saved

print("\n=== csv ===")
p = Path(tempfile.mkdtemp()) / "out.csv"
run(["--api-key", "k", "read", "--csv", str(p)])
run(["--api-key", "k", "read", "--csv", str(p)])
text = p.read_text()
print(text.rstrip())
check("csv header once", text.count("timestamp,sku") == 1)
check("csv two rows", len(text.strip().splitlines()) == 3)
check("csv has co2", ",812," in text)

print("\n=== --raw dump ===")
rc, out, err = run(["--api-key", "k", "read", "--raw"])
check("raw dumps payload", json.loads(out)["capabilities"][0]["instance"] == "online")

print("\n=== device selection ===")
rc, out, err = run(["--api-key", "k", "read", "--sku", "", "--name", "lamp"])
check("non-sensor device yields no values", rc == 0 and "Lamp" in out, out)
rc, out, err = run(["--api-key", "k", "read", "--device", "NO:SUCH"])
check("bad device -> exit 2 + helpful error", rc == 2 and "No device matched" in err, err)

print("\n=== auth / key resolution ===")
import os
os.environ.pop("GOVEE_API_KEY", None)
g.KEY_FILE = Path("/nonexistent/key")
rc, out, err = run(["list"])
check("missing key -> exit 2 with guidance", rc == 2 and "Apply for API Key" in err, err)
os.environ["GOVEE_API_KEY"] = "from-env"
rc, out, err = run(["list"])
check("env var key works", rc == 0, err)

print("\n=== error decoding ===")
try:
    g.GoveeClient._decode('{"code":401,"msg":"invalid key"}', 200)
    check("401 body raises auth error", False)
except g.GoveeAuthError:
    check("401 body raises auth error", True)
try:
    g.GoveeClient._decode("<html>502</html>", 502)
    check("non-JSON raises GoveeError", False)
except g.GoveeError:
    check("non-JSON raises GoveeError", True)

print("\n=== influx line protocol ===")
rc, out, err = run(["--api-key", "k", "read", "--line-protocol"])
line = out.strip()
print(line)
check("measurement name", line.startswith("govee_air_quality,"), line)
check("tags present", "sku=H5140" in line and "device=DE:76:17:29:AA:BB:CC:DD" in line, line)
check("space in tag value escaped", r"name=Study\ CO2" in line, line)
check("int field has i suffix", "co2_ppm=812i" in line, line)
check("float field has no suffix", "temperature_c=21.4" in line, line)
check("temperature field named for its unit", "temperature=" not in line, line)
check("bool coerced to int", "online=1i" in line, line)
check("ns timestamp", len(line.rsplit(" ", 1)[1]) == 19, line)
check("three space-separated sections", len(line.split(" ")) == 3 + line.count(r"\ "), line)

rc, out, err = run(["--api-key", "k", "read", "--line-protocol", "--temp-unit", "f"])
check("fahrenheit gets _f field", "temperature_f=70.52" in out, out)

rc, out, err = run(["--api-key", "k", "read", "--line-protocol",
                    "--influx-measurement", "my,odd meas"])
check("measurement escaped", out.startswith(r"my\,odd\ meas,"), out)

print("\n=== influx writer ===")
posted = []


class FakeResp:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_urlopen(req, timeout=None, context=None):
    posted.append((req.full_url, req.data.decode(), dict(req.headers)))
    return FakeResp()


g.urllib.request.urlopen = fake_urlopen

rc, out, err = run(["--api-key", "k", "read", "-q",
                    "--influx-url", "http://influx.local:8086/", "--influx-db", "govee"])
check("influx read exits 0", rc == 0, err)
check("one point posted", len(posted) == 1, str(posted))
url, body, hdrs = posted[-1]
check("v1 write endpoint", url == "http://influx.local:8086/write?db=govee&precision=ns", url)
check("body is line protocol", body.startswith("govee_air_quality,"), body)
check("no auth header without creds", "Authorization" not in hdrs, str(hdrs))
check("--quiet suppresses stdout", out.strip() == "", out)

posted.clear()
rc, out, err = run(["--api-key", "k", "read", "-q", "--influx-url", "http://influx.local:8086",
                    "--influx-db", "govee", "--influx-user", "u", "--influx-password", "p"])
check("basic auth header set", posted[-1][2].get("Authorization") == "Basic dTpw", str(posted[-1][2]))

posted.clear()
rc, out, err = run(["--api-key", "k", "read", "-q", "--influx-url", "http://influx.local:8086",
                    "--influx-token", "tok", "--influx-org", "o", "--influx-bucket", "b"])
check("v2 endpoint", "/api/v2/write?org=o&bucket=b&precision=ns" in posted[-1][0], posted[-1][0])
check("token header", posted[-1][2].get("Authorization") == "Token tok", str(posted[-1][2]))

rc, out, err = run(["--api-key", "k", "read", "-q", "--influx-url", "http://x:8086",
                    "--influx-token", "tok"])
check("v2 without org/bucket errors clearly", rc == 2 and "influx-org" in err, err)


def failing_urlopen(req, timeout=None, context=None):
    raise g.urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b"db not found"))


g.urllib.request.urlopen = failing_urlopen
rc, out, err = run(["--api-key", "k", "read", "-q", "--influx-url", "http://x:8086",
                    "--influx-db", "nope"])
check("404 explains missing database", rc == 2 and "CREATE DATABASE" in err, err)
g.urllib.request.urlopen = fake_urlopen

print("\n=== device list cache ===")
calls = {"n": 0}
real_devices = g.GoveeClient.devices


def counting_devices(self):
    calls["n"] += 1
    return DEVICES


g.GoveeClient.devices = counting_devices
client = g.GoveeClient("k")
g.CACHE_FILE.unlink(missing_ok=True)
g.load_devices(client, ttl=3600)
g.load_devices(client, ttl=3600)
g.load_devices(client, ttl=3600)
check("cache serves repeat calls from disk", calls["n"] == 1, f"{calls['n']} API calls")
calls["n"] = 0
g.load_devices(client, ttl=0)
g.load_devices(client, ttl=0)
check("ttl=0 disables cache", calls["n"] == 2, f"{calls['n']} API calls")
calls["n"] = 0
g.CACHE_FILE.write_text("{ this is not json")
got = g.load_devices(client, ttl=3600)
check("corrupt cache falls back to API", calls["n"] == 1 and got == DEVICES)
g.GoveeClient.devices = real_devices

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
