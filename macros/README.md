# Creating macros and macro steps

Macros are JSON sequences that drive a claimed station: scan, connect, GATT I/O, waits, asserts, and operator pauses. The engine lives in `app/macros/engine.py`. Built-in macros are Python in `app/macros/builtins.py`. Custom macros are JSON files in this folder.

## Two ways to add a custom macro

### A. From the UI (fastest)

1. Claim a station and open the **Macros** tab.
2. Click **New custom…**. The hub writes `macros/custom-xxxxx.json` with a connect → `read_all` → disconnect stub.
3. Edit the JSON textarea (`id`, `name`, `description`, `params`, `steps`).
4. Click **Save**. The file is rewritten under `macros/<id>.json`.
5. Fill the param fields (they come from `params`) and click **Run**.

Built-ins are read-only. To fork one, use **New custom…** and paste the built-in JSON, or copy a file into this folder.

### B. As a JSON file on disk

1. Create `macros/<id>.json` (see schema below).
2. Restart is not required: `GET /api/macros` reloads every `*.json` in this folder.
3. Invalid JSON is skipped (check the hub log for `skip macro …`).

A user file with the same `id` as a built-in **overrides** that built-in.

```json
{
  "id": "sensor-roundtrip",
  "name": "Trigger then read result",
  "description": "Connect, notify, write, wait, pause, read.",
  "params": {
    "address": "[1]D1:79:29:DB:CB:CC",
    "test_hex": "01"
  },
  "steps": [
    {"op": "connect", "address": "{{address}}"},
    {"op": "notify_on", "uuid": "6e400003-b5a3-f393-e0a9-e50e24dcca9e"},
    {"op": "write", "uuid": "6e400002-b5a3-f393-e0a9-e50e24dcca9e", "hex": "{{test_hex}}"},
    {"op": "wait_notify", "uuid": "6e400003-b5a3-f393-e0a9-e50e24dcca9e", "timeout_ms": 5000, "continue_on_error": true},
    {"op": "read", "uuid": "2a00", "save_as": "name"},
    {"op": "pause", "message": "Press the device button, then Continue"},
    {"op": "read", "uuid": "2a19", "save_as": "battery", "continue_on_error": true},
    {"op": "disconnect"}
  ]
}
```

See [`sensor-roundtrip.json`](sensor-roundtrip.json) in this folder.

### C. HTTP API

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/macros` | Built-ins + user files |
| `GET` | `/api/macros/{id}` | One definition |
| `PUT` | `/api/macros/{id}` | Create or replace a user macro |
| `DELETE` | `/api/macros/{id}` | Delete the JSON file (not built-ins) |
| `POST` | `/api/stations/{station}/macros/{id}/run` | Body: `{ "params": { … } }` |
| `POST` | `/api/stations/{station}/macros/cancel` | Abort the active run |
| `POST` | `/api/stations/{station}/macros/continue` | Resume a `pause` step |
| `GET` | `/api/runs/{run_id}/log` | Download `logs/<run_id>.jsonl` |

`id` is sanitized to `[A-Za-z0-9_-]`. The file name is always `{id}.json`.

---

## Macro schema

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | recommended | Stable key and file stem. Defaults to the file name if omitted. |
| `name` | no | Label in the Macros list. Defaults to `id`. |
| `description` | no | Shown under the name and above the param form. |
| `params` | no | Default run-time values. Each key becomes an input on the Run form. |
| `steps` | yes | Ordered list of step objects. Each step must have `op`. |

Do not set `builtin` in user files; the loader always marks disk macros as custom.

### Params and `{{placeholders}}`

Before each step runs, every string in that step (including nested objects/arrays) is scanned for `{{name}}` (spaces around the name are allowed). Replacement sources, in order:

1. Defaults from `params` in the JSON file.
2. Values from the Run form / API body (override defaults).
3. If the station is already connected and `address` is still empty, `address` is filled with `station.connected_addr`.
4. Values written by earlier steps (`save_as`, `last`, `last_read`, `last_notify`).

Unresolved placeholders are left as the literal `{{name}}`.

Names must match `[A-Za-z0-9_]+`. The UI treats the strings `"true"` / `"false"` as booleans.

The `address` param is special in the UI: if you selected a scan row, that address is pre-filled.

Use BleuIO address form when connecting: `[0]AA:BB:CC:DD:EE:FF` (public) or `[1]…` (random). Copy it from the Scan tab.

### Context written by steps

| Key | When it is set |
| --- | --- |
| `last` | After every successful (or continued) step — the step result object. |
| `last_read` | When the result has a `hex` field (reads, some waits). |
| `last_notify` | After `wait_notify` — notification hex. |
| `{save_as}` | When the step has `"save_as": "foo"` and the result has `hex`. |
| `{save_as}_ascii` | ASCII preview of that same result. |

Example: read into a name, then assert or log it later:

```json
{"op": "read", "uuid": "2a00", "save_as": "dev_name"}
```

Later steps can use `{{dev_name}}` or `{{dev_name_ascii}}`.

---

## Fields that apply to every step

| Field | Type | Meaning |
| --- | --- | --- |
| `op` | string | Operation name. Unknown ops fail the run. |
| `timeout_s` | number | Hard cap for the whole step. Default `BLEUIO_MACRO_STEP_TIMEOUT` (30 s). |
| `continue_on_error` | bool | If true, a failed step is logged and the run continues. Default: abort. |
| `save_as` | string | Store result `hex` / `ascii` into the substitution context. |

A step is a failure when it raises, times out, returns `ok: false`, or returns an `error` string.

Only one macro can run on a station. Scan / GATT API calls return **409** while a run is active — cancel first.

Each run writes `logs/<run_id>.jsonl` (`started`, `step_started`, `step_result` / `step_error`, `finished`).

---

## Step operations

Target characteristics with `"uuid"` (16-bit like `"2a00"` or a 128-bit UUID) or `"handle"` (hex handle from the GATT tree). UUID match is suffix-tolerant (`2a00` matches the full SIG UUID).

### `log`

Write a message into the run log / UI. Does not talk to the dongle.

```json
{"op": "log", "message": "Starting notify loop for {{address}}"}
```

`text` is accepted as an alias for `message`.

### `delay`

Sleep. Duration is milliseconds (`timeout_ms` or `ms`). Default 1000.

```json
{"op": "delay", "ms": 250}
```

Note: `timeout_s` is the engine’s **deadline** for the step, not the sleep length. A 40 s sleep needs `"timeout_s": 45` (or a higher `BLEUIO_MACRO_STEP_TIMEOUT`).

### `pause`

Block until the operator clicks **Continue** (or the run is cancelled). Use for physical actions (button press, move the DUT, enter a passkey in the UI).

```json
{"op": "pause", "message": "Press the device button, then Continue"}
```

### `scan`

Start `AT+FINDSCANDATA`, wait `duration` seconds (or `timeout_s`, default 5), then stop. Optional `filter` is a hex substring passed to the dongle.

```json
{"op": "scan", "duration": 8, "filter": ""}
```

Cannot run while connected. Results are the current `station.devices` map.

### `connect`

```json
{"op": "connect", "address": "{{address}}"}
```

`address` comes from the step, then from context. Missing address fails. After connect the hub browses GATT; later `read` / `write` / notify steps need that tree.

### `disconnect`

```json
{"op": "disconnect"}
```

### `read`

```json
{"op": "read", "uuid": "2a19", "save_as": "battery"}
```

Result typically includes `ok`, `hex`, `ascii`.

### `write` and `write_cmd`

`write` is a request/response write. `write_cmd` is write-without-response (or set `"without_response": true` on `write`).

Hex payload (default if the step has a `hex` key, or has no `ascii` key):

```json
{"op": "write", "uuid": "6e400002-b5a3-f393-e0a9-e50e24dcca9e", "hex": "{{test_hex}}"}
```

ASCII payload:

```json
{"op": "write", "uuid": "2a00", "ascii": "Hello"}
```

`data` is an alias for the ASCII value.

### `notify_on` / `notify_off` / `indicate_on` / `indicate_off`

```json
{"op": "notify_on", "uuid": "6e400003-b5a3-f393-e0a9-e50e24dcca9e"}
{"op": "indicate_off", "handle": "0012"}
```

Enable notify/indicate **before** `wait_notify`.

### `wait_notify`

Wait for the next notification/indication event, up to `timeout_ms` (default 5000).

```json
{
  "op": "wait_notify",
  "uuid": "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
  "timeout_ms": 5000,
  "contains": "01"
}
```

- `uuid` / `handle` is a best-effort filter (mismatch is still accepted).
- `contains` is a hex substring (spaces ignored). Missing bytes fail the step.
- Sets `last_notify` and, on success, result `hex` / `ascii` (so `save_as` works).
- This wait uses `timeout_ms`. Keep `timeout_s` larger than `timeout_ms / 1000` or the engine deadline fires first.

### `wait_adv`

Start a scan if needed and wait until an advertisement matches.

```json
{"op": "wait_adv", "address": "{{address}}", "contains": "ff4c00", "timeout_ms": 8000}
```

- `contains`: hex substring of `adv_hex + scan_rsp_hex`.
- `address`: optional; matched as a case-insensitive substring of the device address.
- Default timeout 8000 ms. Stops the scan on match or timeout.

### `read_all`

Read every characteristic whose property flags include Read. Others are logged as skipped.

```json
{"op": "read_all"}
```

Requires an active connection (and a GATT tree).

### `write_all`

Write `hex` (default `"00"`) to every writable characteristic. If `restore` is true (or `"true"` / `"1"` / `"yes"`), readable chars are read first and written back after the test byte.

```json
{"op": "write_all", "hex": "00", "restore": true}
```

Uses write-without-response only when that is the sole write property.

### `probe_permissions`

Try read and write on **every** characteristic and compare success to advertised property flags. Logs mismatches. Attempts to restore the original value after a successful write.

```json
{"op": "probe_permissions", "hex": "00"}
```

### `verify_adv`

Assert fields on a device already present in the last scan results (run `scan` or `wait_adv` first).

```json
{
  "op": "verify_adv",
  "address": "{{address}}",
  "name": "MySensor",
  "uuids": ["180f", "180a"],
  "company_id": "0x0059",
  "contains": "020106"
}
```

| Field | Check |
| --- | --- |
| `address` | Pick that device from scan cache. Falls back to the sole scanned device. |
| `name` | Case-insensitive exact match of advertised name. |
| `uuids` | Array, or comma-separated string. Each normalized UUID must appear in advertising. |
| `company_id` | Hex (optional `0x`) must appear somewhere in the device JSON. |
| `contains` | Hex substring of `adv_hex + scan_rsp_hex`. |

Empty optional fields are skipped. Failure sets `ok: false` and a joined `error` string.

### `advertised_vs_gatt`

After connect (scan data still in cache), compare advertised UUIDs to the GATT tree. Reports advertised-but-missing and GATT-not-advertised (excluding Generic Access/Attribute 0x1800/0x1801).

```json
{"op": "advertised_vs_gatt"}
```

### `assert`

Exactly one of the following forms:

**Hex equality** (default source `last_read`):

```json
{"op": "assert", "equals": "64", "source": "battery"}
```

**Hex substring**:

```json
{"op": "assert", "contains": "4c00", "source": "last_read"}
```

`source` is a context key. Comparison is case-insensitive; spaces in `equals` / `contains` are stripped.

**Advertised UUID present**:

```json
{"op": "assert", "adv_uuid": "180f"}
```

**GATT properties** on a characteristic (`R` read, `W` write, `X` write-without-response, `N` notify, `I` indicate):

```json
{"op": "assert", "uuid": "2a19", "properties": "RN"}
```

### `wait_passkey`

No-op marker. Pairing still uses the UI passkey modal. Place a `pause` after connect if the operator must type a key before the next GATT step.

```json
{"op": "wait_passkey"}
{"op": "pause", "message": "Enter the 6-digit passkey in the dialog, then Continue"}
```

---

## Authoring a sequence

Typical patterns:

1. **Scan then verify advertising** — `scan` → `verify_adv` (no connect).
2. **Connect then inventory** — `connect` → `read_all` / `probe_permissions` / `advertised_vs_gatt` → `disconnect`.
3. **Command / notify** — `connect` → `notify_on` → `write` → `wait_notify` → `assert` → `notify_off` → `disconnect`.
4. **Human in the loop** — insert `pause` wherever the DUT needs a physical action.

Checklist:

- Claim the station before Run.
- Prefer UUIDs over handles (handles change per connection / firmware).
- Put `notify_on` before `wait_notify`.
- Give waits a `timeout_ms` and a larger `timeout_s` if the wait is long.
- Use `continue_on_error` only on steps that are allowed to fail (optional characteristics).
- Disconnect at the end so the next user (or the next scan) is not blocked.

---

## Adding a new built-in macro

Edit `app/macros/builtins.py`. Each entry is the same shape as a JSON file, plus `"builtin": True`.

```python
{
    "id": "read-battery",
    "name": "Read battery",
    "description": "Connect and read Battery Level (0x2A19).",
    "builtin": True,
    "params": {"address": ""},
    "steps": [
        {"op": "connect", "address": "{{address}}"},
        {"op": "read", "uuid": "2a19", "save_as": "battery"},
        {"op": "disconnect"},
    ],
}
```

`id` must be unique among built-ins. A user file with the same `id` still overrides it at runtime.

Restart the hub (or reload the process) after changing Python. JSON files in `macros/` do not need a restart.

---

## Adding a new step operation (`op`)

Use this when existing ops cannot express the action (new AT command, new assert, multi-characteristic helper).

1. **Implement the handler** in `MacroEngine._run_step` in `app/macros/engine.py`.
   - Return a `dict` with `"ok": True` or `"ok": False`.
   - On failure, set `"error": "…"` so the UI and JSONL log show the reason.
   - Raise `RuntimeError` for programming / precondition errors (missing address, unknown op).
   - If the result should be reusable, include `"hex"` (and optionally `"ascii"`) so `save_as` / `last_read` work.
   - Keep the function async if it calls `station.*` or sleeps.

2. **Reuse station APIs** on `app.dongle.base.Station` (`connect`, `read`, `write`, `set_notify`, `start_scan`, …) instead of talking to the serial port from the engine.

3. **Document the op** in the table above (this file) and mention it in the main README step-op list.

4. **Add a built-in or a sample JSON** that uses the new op so it is visible in the Macros tab.

5. **Timeouts**: the engine wraps every step in `asyncio.wait_for(..., timeout_s)`. Long waits should read `timeout_ms` internally (like `wait_notify`) and document that `timeout_s` must be greater.

6. **Events**: `wait_notify` / `wait_adv` subscribe to station events (`notify`, `scan` / `adv`). New wait-style ops should use the same `notify_waiters` / `adv_waiters` lists (or add a third list) so they see live events, not a poll of stale state.

7. **Do not** put new ops only in the UI. The UI is a JSON editor; the engine is the source of truth.

Minimal handler sketch:

```python
if op == "my_op":
    target = step.get("uuid") or step.get("handle")
    if not target:
        raise RuntimeError("my_op requires uuid or handle")
    result = await station.read(str(target))
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "my_op failed"}
    return {"ok": True, "hex": result.get("hex"), "ascii": result.get("ascii")}
```

Unknown `op` values fail with `Unknown macro op: …`.

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Macro missing from the list | Invalid JSON, or file not named `*.json` under `macros/`. |
| `{{address}}` left unsubstituted | Param name mismatch, or the field was never in `params` / the Run form. |
| Connect fails | Address missing `[0]`/`[1]` prefix, or no claim on the station. |
| Read/write “not found” | Not connected, or UUID/handle does not match the browsed GATT tree. |
| `wait_notify` times out | Forgot `notify_on`, wrong characteristic, or `timeout_s` shorter than `timeout_ms`. |
| Step aborted at 30 s | Raise `timeout_s` on that step or set `BLEUIO_MACRO_STEP_TIMEOUT`. |
| 409 on scan/GATT buttons | A macro is still `running` or `paused` — Cancel or Continue. |
| Built-in JSON not editable | Duplicate via **New custom…** or add a file in this folder. |
