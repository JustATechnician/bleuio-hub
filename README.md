# BleuIO Hub

LAN web app for a Raspberry Pi 4 with one or more [BleuIO](https://www.bleuio.com/) USB dongles. Each user claims a dongle exclusively, then scans BLE peripherals, inspects advertised data, connects, and reads/writes/notifies GATT characteristics. Macros automate those checks and log the results.

Designed for 4 concurrent users today; it enumerates however many dongles are plugged in.

## Features

- Claim / release stations with a 5-minute idle timeout
- Live scan with decoded advertising (LTV: flags, name, UUIDs, manufacturer data) plus raw hex
- GATT explorer: SIG names, handles, property flags, read / write / notify / indicate
- Advertised-vs-GATT UUID hints after connect
- Passkey prompt when the peer requests pairing
- Built-in macros: probe permissions, read all, write-all test, verify advertising, advertised vs GATT
- Custom JSON macros with delay, pause (operator Continue), wait-for-notify, asserts, and JSONL logs

## Quick start (development / Windows)

Python 3.9+ (3.11 recommended on the Pi).

```bash
cd bleuio-hub
python -m venv .venv
# Windows Git Bash:
source .venv/Scripts/activate
# Linux / Pi:
# source .venv/bin/activate
pip install -r requirements.txt
# Git Bash:
export BLEUIO_MOCK=1
# Windows cmd:
# set BLEUIO_MOCK=1
python -m app
```

Open http://127.0.0.1:8000

With `BLEUIO_MOCK` unset, the hub opens any BleuIO serial ports it finds and **falls back to 4 mock stations** if none are present. Set `BLEUIO_MOCK=0` to disable that fallback.

## Raspberry Pi 4

1. Copy this repo to the Pi (for example `/home/pi/bleuio-hub`).
2. Add the service user to `dialout` and log out/in:

   ```bash
   sudo usermod -aG dialout $USER
   ```

3. **Disable ModemManager** so it does not grab CDC ACM ports:

   ```bash
   sudo systemctl disable --now ModemManager.service
   ```

4. Optional: install udev rules so BleuIO TTYs are always in `dialout`:

   ```bash
   sudo cp deploy/99-bleuio.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   ```

5. Create a venv and install deps:

   ```bash
   cd /home/pi/bleuio-hub
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

6. Plug in the dongles. Wait ~15 seconds after insert (bootloader COM port, then application). Confirm with `lsusb` — application firmware shows `2dcf:6002`. Ports are typically `/dev/ttyACM*`.

7. Run:

   ```bash
   python -m app
   ```

   Or install the systemd unit (edit `User` / `WorkingDirectory` if needed):

   ```bash
   sudo cp deploy/bleuio-hub.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now bleuio-hub
   ```

8. On the LAN, open `http://<pi-ip>:8000`.

Use a **powered USB hub** if four dongles starve the Pi’s USB ports.

Firmware: BleuIO Standard 2.2.1 or later (2.7.x recommended). BleuIO Pro is supported by the Python library.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `BLEUIO_HOST` | `0.0.0.0` | Bind address |
| `BLEUIO_PORT` | `8000` | HTTP port |
| `BLEUIO_MOCK` | unset | `1` force mock; `0` real only; unset = real then mock fallback |
| `BLEUIO_IDLE_TIMEOUT` | `300` | Seconds until an idle claim is released |

## Macros

Built-in macros appear in the Macros tab. Custom macros are JSON files in `macros/`. Example: [`macros/sensor-roundtrip.json`](macros/sensor-roundtrip.json).

**Authoring guide:** [macros/README.md](macros/README.md) — how to add a custom macro (UI, file, or API), every step `op` and its fields, placeholders, logs, and how to add a new step type in the engine.

Step ops: `scan`, `connect`, `disconnect`, `read`, `write`, `write_cmd`, `notify_on` / `notify_off`, `indicate_on` / `indicate_off`, `read_all`, `write_all`, `probe_permissions`, `verify_adv`, `advertised_vs_gatt`, `delay`, `pause`, `wait_notify`, `wait_adv`, `wait_passkey`, `assert`, `log`.

`{{param}}` placeholders are substituted from the run form. `pause` waits until Continue in the UI. Each run writes `logs/<run_id>.jsonl`.

## Layout

- `app/` — FastAPI backend, dongle workers, macro engine, static UI
- `macros/` — user JSON macros and the [authoring guide](macros/README.md)
- `logs/` — run logs (gitignored)
- `deploy/` — systemd unit and udev rules
