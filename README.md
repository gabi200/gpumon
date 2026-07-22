# gpumon

Cross-vendor (**NVIDIA + AMD**) GPU monitor for Linux with logging, an HTTP /
Prometheus API, and **early-failure detection** aimed at predictive maintenance
(failing fans, degraded silicon, throttling, cooling problems).

Pure Python standard library — **no dependencies required**. `pynvml` is used
automatically if present, otherwise NVIDIA is read via `nvidia-smi`. AMD is read
directly from the `amdgpu` sysfs/hwmon interface (no `rocm-smi` needed), which
also covers integrated Radeon GPUs.

## What it records

Per GPU, every poll: **temperature** (edge + hotspot + memory), **board power**
draw and limit, **GPU load**, **core/memory clock** (current + max), **fan**
speed (rpm and %), and voltage. Sensors a card doesn't expose are reported as
`null` (not a fake 0), so an iGPU with no fan is handled gracefully.

Everything is written to a **SQLite** database (`samples`, `alerts`,
`baselines` tables) with automatic retention pruning.

## Early-failure detection

The health engine learns a per-GPU **baseline** of the best clock and power the
card reaches *while genuinely under load*, persists it across restarts, and
alerts when the card can no longer meet it:

| Alert | Meaning |
|-------|---------|
| `clock_shortfall` | Under load the core clock is well below the learned/rated max — thermal throttling, degraded silicon, or a stuck power state |
| `power_shortfall` | Under load the board can't draw the power it used to |
| `fan_stopped` | Fan reads 0 while the GPU is hot — likely a failed/seized fan |
| `overtemp` / `hotspot` | Temperature past warn/critical thresholds — cooling problem |

You can also pin expected values so detection works from the first sample
instead of waiting to learn. Thresholds are all configurable.

## Spec enrichment (where "expected" comes from)

On first sight of each GPU, gpumon resolves its rated clock/power and pins them
as the expected reference. Resolution order, highest confidence first:

1. **`expected` in config** — explicit per-GPU values you set.
2. **`specs_file`** — a JSON of overrides keyed by PCI id (`vendor:device`) or
   model name (see `specs.example.json`). Ideal for APUs / OEM parts.
3. **[dbgpu](https://github.com/painebenjamin/dbgpu)** — optional offline spec
   database (`pip install dbgpu`); looked up by model name, provides TDP + boost
   clock for ~2000 discrete cards.
4. **Driver self-reported max** — NVIDIA `clocks.max.gr` / AMD `pp_dpm_sclk`,
   already read every poll. Most reliable for clock; the usual fallback.
5. **Learned baseline** — the best clock/power seen under load over time.

Model names for AMD are resolved from the system `pci.ids` file (or `lspci`);
NVIDIA reports its marketing name directly. The resolved model and rated numbers
appear under each GPU's `spec` field in the API and in `--once` output, e.g.:

```
[amd:0] amdgpu 0x15e7 (card1)  ->  Barcelo (rated 1800MHz/30W, src=specs_file)
```

```bash
python3 -m gpumon --once --specs-file specs.example.json
python3 -m gpumon --no-enrich          # disable resolution entirely
```

> Spec-DB numbers are reference-design figures; real boost varies with
> GPU Boost / board OC / silicon. They seed the *expected* value, which the
> health engine compares against with a tolerance percentage — the learned
> baseline still refines it over time.

## Usage

```bash
# one-shot snapshot of every GPU
python3 -m gpumon --once

# run the monitor + API (Ctrl-C to stop)
python3 -m gpumon --config config.example.json
python3 -m gpumon --interval 5 --db gpumon.db --port 8642

# live terminal dashboard (curses, no server)
python3 -m gpumon --tui

# monitor without the HTTP server
python3 -m gpumon --no-api
```

### Live dashboard

`python3 -m gpumon --tui` opens a full-screen dashboard with per-GPU bar gauges
for temperature, load, clock, power and fan, colour-coded health state, and any
active alerts listed inline. Press `q` (or Ctrl-C) to quit. It polls locally and
needs no running server.

```
 gpumon  ·  1 GPU(s)  ·  backends: amd  ·  uptime 12s
 2026-07-22 11:35:15   (press q to quit)

 [amd:0] amdgpu 0x15e7 (card1)                                        OK
   temp  ███████████░░░░░░░░░░░░░ 46C
   load  █░░░░░░░░░░░░░░░░░░░░░░░ 3%
   clock █████░░░░░░░░░░░░░░░░░░░ 400MHz / 1800MHz  mem 1200MHz
   power ████████████████████████ 15W
   fan   ························ n/a / n/a
```

### API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness + per-GPU health state (HTTP 503 if any GPU critical) |
| `GET /gpus` | Latest reading for every GPU |
| `GET /gpus/<key>` | One GPU, e.g. `/gpus/amd:0`, `/gpus/nvidia:0` |
| `GET /history?gpu=<key>&limit=N` | Recent samples from the database |
| `GET /alerts?limit=N` | Recent alerts |
| `GET /metrics` | Prometheus text exposition (scrape into Grafana/Alertmanager) |

```bash
curl -s localhost:8642/gpus | python3 -m json.tool
curl -s localhost:8642/metrics
```

## Central fleet dashboard (MQTT + Grafana)

For monitoring many machines, each node publishes over **MQTT** to a central
stack that aggregates the fleet and visualises it in **Grafana**:

```
node 1: gpumon --mqtt-host BROKER ─┐
node 2: gpumon --mqtt-host BROKER ─┼─▶ Mosquitto ─▶ gpumon-bridge ─▶ Prometheus ─▶ Grafana
node N: gpumon --mqtt-host BROKER ─┘   (broker)     (MQTT→metrics)                 (dashboard)
```

`gpumon-bridge` subscribes to every node's telemetry, keeps the latest reading
per GPU, and re-exposes it on one Prometheus endpoint with `node` / `gpu` /
`vendor` labels — so the same metric names work whether you scrape one machine
or a hundred.

### Central host (one command)

```bash
cd deploy/central
docker compose up -d
```

Brings up Mosquitto (`:1883`), the bridge (`:9109/metrics`), Prometheus
(`:9090`), and Grafana (`:3000`, admin/admin) with the **gpumon — GPU Fleet**
dashboard already provisioned (fleet health stats, temperature, utilization,
clock-vs-expected, power-vs-limit, fan, and a live active-alerts table, filtered
by a `$node` selector).

### Each node

```bash
pip install paho-mqtt
python3 -m gpumon --mqtt-host <central-host> --mqtt-node gpu-rig-01
```

or in `config.json`:

```json
"mqtt": { "host": "central-host", "node": "gpu-rig-01", "base": "gpumon" }
```

MQTT topics (base `gpumon`):

| Topic | Payload |
|-------|---------|
| `gpumon/<node>/status` | retained `online`/`offline` (LWT-backed liveness) |
| `gpumon/<node>/<gpu>` | JSON telemetry, one message per GPU per poll |
| `gpumon/<node>/alerts` | JSON alert events as they fire |

If `paho-mqtt` isn't installed or the broker is unreachable, the node logs a
warning and keeps monitoring/serving locally — MQTT is purely additive.

## Running as a systemd service

An installer and a hardened unit file are provided under `deploy/`:

```bash
sudo ./deploy/install.sh            # install + enable + start
sudo ./deploy/install.sh uninstall  # stop + remove (keeps config/db)
```

The installer copies the package to `/opt/gpumon`, writes
`/etc/gpumon/config.json` (with the database pointed at `/var/lib/gpumon`),
installs `deploy/gpumon.service`, and enables it. Then:

```bash
systemctl status gpumon       # service state
journalctl -u gpumon -f       # live logs and alerts
curl localhost:8642/health    # API
```

The unit runs under a `DynamicUser` in the `video`/`render` groups (enough to
read amdgpu sysfs and NVML) with `NoNewPrivileges`, `ProtectSystem=strict` and a
writable `StateDirectory`. If some sensors still read as `null`, check
permissions on `/sys/class/drm/card*/device`.

## Layout

```
gpumon/
  backends/       amd.py (sysfs), nvidia.py (pynvml/nvidia-smi), base.py
  storage.py      SQLite: samples, alerts, learned baselines
  health.py       baseline learning + failure detection
  api.py          stdlib HTTP server + Prometheus exporter
  monitor.py      poll -> store -> evaluate -> expose loop
  cli.py          command line entry point
config.example.json
```
