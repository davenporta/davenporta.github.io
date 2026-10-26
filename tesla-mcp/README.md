# tesla-mcp: self-hosted Tesla Fleet API MCP server (maintenance + climate only)

A free, self-hosted alternative to Tessie/Teslemetry for **reading maintenance info** and
**controlling climate**. It talks directly to Tesla's official Fleet API, so the only cost is
Tesla's per-request billing, which the $10/month account discount covers for one car (see Cost).

* Transports: **stdio (primary)**, plus streamable HTTP (`--transport http`, 127.0.0.1 by default).
* Read-only tools never wake the car. Waking is a separate, explicit, confirm-gated tool.
* Every command tool returns a **preview** unless called with `confirm: true`.
* Climate commands go **only** through Tesla's signed-command proxy (`tesla-http-proxy`).
* There is a **hard endpoint allowlist** in the HTTP client (`tesla_mcp/client.py`). There is no lock/unlock,
  door/trunk/frunk/window, remote start, honk/flash, charge-port/charging, Sentry, valet, or any
  other non-climate command, and none can be reached even by crafting paths.

## Tools

| Tool | What | Cost* |
|---|---|---|
| `list_vehicles` | VINs, names, online/asleep state (no wake) | free |
| `daily_check(vin, battery_threshold_pct=30, tire_tolerance_psi=3)` | compact **needs-attention** list: new alerts since last run, service status, key pairing, and (only if the car is already online or data is cached) battery < threshold, tires ≥3 psi below recommended or TPMS warning, update waiting on Wi-Fi. Reports `asleep`. **Never wakes.** | free, or $0.002 if online |
| `charge_check(vin, threshold_pct=50)` | battery below threshold and not charging? **Never wakes**; asleep + uncached → `needs_charge: null` | free, or $0.002 if online |
| `vehicle_status(vin, include_location=false)` | battery, range, charging, locked, odometer, version. Location **only** if `include_location=true` | $0.002 |
| `maintenance_summary(vin)` | odometer, tire pressure + TPMS warnings, software update, battery figures, service mode, service status, recent alerts, and a **"not exposed"** list | $0.002 |
| `battery_health(vin)` | level / usable level / ranges + projected range at 100% (rough proxy; **no true SoH**) | $0.002 |
| `vehicle_alerts(vin)` | `GET /recent_alerts` | free |
| `software_update_status(vin, include_pending_update=false)` | installed firmware + key pairing (free fleet_status); pending update from cache, or one vehicle_data if requested | free / $0.002 |
| `climate_status(vin, include_schedules=false)` | temps, set points, defrost/seat/wheel heater, preconditioning schedules | $0.002 |
| `wake_vehicle(vin, confirm)` | wake a sleeping car, then poll the free state endpoint every 5 s for ≤60 s (never re-wakes) | $0.02 |
| `start_climate`, `stop_climate` | auto_conditioning_start/stop | $0.001 |
| `set_temps(vin, driver_temp_c, passenger_temp_c?)` | °C, 15–28 | $0.001 |
| `set_seat_heater(vin, seat, level 0-3)` | needs climate on | $0.001 |
| `set_steering_wheel_heater(vin, on)` | needs climate on | $0.001 |
| `set_defrost(vin, on)` | `set_preconditioning_max` (max defrost) | $0.001 |
| `climate_actions(vin, actions[], wake_if_asleep=false)` | batch up to 10 climate steps after at most one wake | $0.001/step (+$0.02 if woken) |
| `add_precondition_schedule(vin, time_hhmm, days, latitude, longitude, enabled=true, one_time=false)` | recurring scheduled-departure preconditioning, e.g. `days=["WEEKDAYS"]`, `"07:30"` (firmware 2024.26+) | $0.001 |
| `remove_precondition_schedule(vin, schedule_id)` | delete schedule | $0.001 |
| `send_destination(vin, address? , latitude?, longitude?)` | send a destination to the car's nav (`navigation_request` share) | $0.001 |
| `next_trip_check(vin, arrival_time, destination? , latitude?, longitude?, title?)` | caller-supplied destination (address or lat/lon) + arrival time (ISO; no offset = configured local timezone), e.g. taken from any calendar by the caller — this server reads no calendar → OSM geocode (cached) → OSRM drive time (fallback haversine×1.3 @55 mph) → `reasonable` to offer preconditioning? + suggested leave / precondition-start (leave − 15 min) and reasons. Read-only; never wakes | free, or $0.002 if online |

\*Free: `GET /vehicles`, `GET /vehicles/{vin}` (state), `fleet_status`, `recent_alerts`, `service_data`,
`release_notes`. Paid: `vehicle_data` $0.002, commands $0.001, `wake_up` $0.02.
All command tools are **preview-only unless `confirm: true`**. Before a confirmed command the server checks virtual-key
pairing (free `fleet_status`, cached 1 h; refuses with "pair the key first") and car state (free), so it never pays for
a 408. `vehicle_data` is fetched once for all sections and cached per car for 3 minutes, shared by every read tool.
`send_destination` is the only non-climate command allowed; `navigation_gps_request` / waypoints / supercharger
navigation and everything else stay blocked (proxy v0.4.1 can't sign `navigation_gps_request`; coordinates are sent
as `"lat,lon"` text via `navigation_request`, which the proxy forwards to Fleet API).

### Safety guards (enforced in code)
* **Low battery:** anything that turns climate/preconditioning *on* (start_climate, set_temps, seat/wheel heat > off,
  defrost on, climate_actions batches with any of those, add_precondition_schedule) is **refused** below
  `TESLA_MIN_CLIMATE_SOC` (default 20%) and allowed **with a warning** below `TESLA_WARN_CLIMATE_SOC` (default 30%) —
  in the preview and at send time. If the battery is unknown because the car is asleep (no fresh cache), it is refused
  rather than waking the car to check. Turning things off and removing schedules are always allowed.
* **Away from home/work:** the same actions are refused when the car is more than `TESLA_PLACE_RADIUS_KM` (1 km) from
  `TESLA_HOME_LATLON` / `TESLA_WORK_LATLON` ("lat,lon"; or `~/.config/tesla-mcp/places.json` — keep real coordinates
  out of git in `~/.config/tesla-mcp/places.json`). Needs the `vehicle_location` OAuth
  scope; without it the location is unknown → allowed with a warning (`TESLA_REQUIRE_LOCATION=1` to refuse instead).
  `daily_check` reports `at_home_or_work`, `precondition_allowed`, `precondition_reason`.
* `next_trip_check` limits: `TESLA_MAX_PRECOND_DRIVE_MIN` (90), range ≥ 1.5 × trip + `TESLA_RANGE_MARGIN_MI` (20),
  `TESLA_TRIP_BUFFER_MIN` (10) added before the arrival time.
* Charging commands are not on the endpoint allowlist at all (tested).

### What Tesla does NOT expose (honest list)
* Service history / past visits / invoices (only *current* service status via `service_data`)
* **Battery state-of-health or degradation %** (no such field). `battery_health` reports
  `battery_range / battery_level × 100` as a rough, noisy proxy; track it over time and compare to your original EPA range.
* Tire tread depth, brake wear, filter/fluid service intervals, 12V battery health metric, recalls/warranty.

## Cost (verified 2026-10-23)
Source: <https://developer.tesla.com/docs/fleet-api/billing-and-limits>, <https://developer.tesla.com/>

| Category | Price |
|---|---|
| Data requests | 500 per $1 → $0.002 each |
| Commands | 1,000 per $1 → $0.001 each |
| Wakes | 50 per $1 → $0.02 each |
| Streaming signals | 150,000 per $1 (not used) |

* **$10 monthly discount per account** ("roughly covers ... 100 commands, and 2 wakes per day for two vehicles").
* Any response with status < 500 is billable (including 408 "asleep"). 5xx responses are not billed.
* **Default billing limit is $0.** Apps that exceed their limit **or have no payment method** get disabled.
  Add a payment method in the developer dashboard; you can keep the limit low (e.g. $1–5).
* Rate limits per car: data 60/min, commands 30/min, wakes 3/min.

**Example (1 car):** daily_check once a day ≤ $0.06/month; 4 data reads/day = 120/month = $0.24; 1 wake/day = $0.60;
~5 climate commands/day = 150 = $0.15. **≈ $1/month, fully covered by the $10 discount.**
Wakes are the main cost driver. Let the car sleep.

## Setup (in order)

> Steps marked 👤 can only be done by you (they need your Tesla login, phone, car, payment method, or GitHub account).

1. **Install**
   ```bash
   python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[test]'
   pytest -q                       # offline, no credentials needed
   ```
2. **Generate the key pair** (already done on the build box: `keys/private-key.pem`, mode 0600):
   `scripts/gen_keys.sh`. Never commit or share `private-key.pem`. Back it up; it's what your car trusts.
3. 👤 **Host the public key on GitHub Pages (free)** at your domain `<your-github-username>.github.io`:
   * Create the **public** repo `<your-github-username>.github.io` on GitHub (empty, no README).
   * Push the prepared repo: `cd pages-repo && git remote add origin https://github.com/<your-github-username>/<your-github-username>.github.io.git && git push -u origin main`
     (it contains only `.well-known/appspecific/com.tesla.3p.public-key.pem`, `.nojekyll`, `index.html`).
   * Repo **Settings → Pages → Build and deployment → Deploy from a branch → `main` / `(root)`** → Save.
   * Verify (allow a few minutes):
     `curl -fsS https://<your-github-username>.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem | diff - keys/public-key.pem && echo OK`
   * `.nojekyll` is required; otherwise Jekyll drops the dot-folder.
4. 👤 **Tesla developer account and app** at <https://developer.tesla.com> (Tesla account with verified email + MFA):
   * Fill in your name / business details (an individual is fine), app name (must be unique, or the request may be auto-rejected),
     description, purpose ("personal use: read my car's maintenance info and control climate").
   * OAuth grant type: **Authorization code and machine-to-machine**.
   * Allowed origin: `https://<your-github-username>.github.io`
   * Redirect URI: `https://<your-github-username>.github.io/callback` (it 404s; that's fine, since you copy the `code` from the address bar).
   * Scopes: **Vehicle Information** (`vehicle_device_data`) and **Vehicle Commands** (`vehicle_cmds`); optionally
     **Vehicle Location** (`vehicle_location`) if you want `include_location`. Don't request charging or user-data scopes.
   * 👤 Add a **payment method** and set a small **billing limit** (the default $0 limit disables the app).
   * Note the Client ID and Client Secret (keep the secret out of shell history; use an env file with 600 perms).
5. **Environment** (e.g. `~/.config/tesla-mcp/env`, `chmod 600`):
   ```bash
   export TESLA_CLIENT_ID=...
   export TESLA_CLIENT_SECRET=...           # only needed by the setup script
   export TESLA_REGION=na                   # na (N. America + APAC), eu (EMEA), cn
   export TESLA_REDIRECT_URI=https://<your-github-username>.github.io/callback
   export TESLA_DOMAIN=<your-github-username>.github.io
   export TESLA_PROXY_URL=https://127.0.0.1:4443
   export TESLA_PROXY_CA_CERT=$PWD/proxy-config/tls-cert.pem
   # optional: TESLA_TOKEN_FILE (default ~/.config/tesla-mcp/tokens.json), TESLA_FLEET_BASE_URL, TESLA_HTTP_TIMEOUT
   # optional bootstrap: TESLA_REFRESH_TOKEN (the token file takes precedence once it exists)
   ```
6. **Register your domain with Fleet API** (once per region): `python scripts/tesla_setup.py register`,
   then `python scripts/tesla_setup.py check-public-key` should echo your public key.
7. 👤 **Log in and consent**: `python scripts/tesla_setup.py authorize-url` → open the URL, log in, approve →
   copy the full redirected URL → `python scripts/tesla_setup.py exchange '<url-or-code>'`.
   Tokens are saved to the 0600 token file. Refresh tokens rotate on each use and expire after 3 months of disuse;
   the server persists each new one automatically.
8. 👤 **Pair the virtual key with your car**: `python scripts/tesla_setup.py pairing-link --vin <VIN>`, open
   `https://tesla.com/_ak/<your-github-username>.github.io` on your phone (Tesla app 4.27.3+), near the car, and approve.
   (Remove it later from the car's **Controls → Locks** screen.)
9. **Run the signed-command proxy** (required for climate/navigation commands). Build pinned v0.4.1 from source
   (`go install ...@v0.4.1` fails because of replace directives):
   ```bash
   git clone --depth 1 --branch v0.4.1 https://github.com/teslamotors/vehicle-command ~/src/vehicle-command
   (cd ~/src/vehicle-command && go build -o ~/.local/bin/tesla-http-proxy ./cmd/tesla-http-proxy)
   scripts/gen_proxy_tls.sh            # self-signed localhost/127.0.0.1 cert in proxy-config/ (git-ignored)
   scripts/run_proxy.sh start          # 127.0.0.1:4443 only; start|stop|restart|status|foreground
   ```
   `run_proxy.sh` runs a restart-on-exit supervisor (setsid/nohup + flock) using `keys/private-key.pem`; logs and the
   session cache go to `~/.local/state/tesla-mcp/` (proxy.log, rotated at 5 MB; no `-verbose`, so no bodies or tokens).
   It never binds 0.0.0.0. `run_mcp.sh` starts it automatically if it isn't running (e.g. after a reboot).
10. **Add to your MCP client (stdio)**: the command is just `scripts/run_mcp.sh` (it sets the non-secret env from
    `scripts/env.sh`, ensures the proxy is up, then execs `.venv/bin/tesla-mcp`):
    ```json
    {"mcpServers": {"tesla": {"command": "/workspace/tesla-mcp/scripts/run_mcp.sh",
      "env": {"TESLA_CLIENT_ID": "..."}}}}
    ```
    The running server doesn't need the client secret (refresh uses client_id + refresh token from
    `~/.config/tesla-mcp/tokens.json`). HTTP alternative: `run_mcp.sh --transport http --port 8765` (loopback).
    Binding to a non-loopback host requires `TESLA_MCP_HTTP_TOKEN` (Bearer auth).

## CLI for scheduled jobs
`scripts/tesla_cli.sh` (same env as `run_mcp.sh`; needs `TESLA_CLIENT_ID`) prints JSON. It only runs read-only checks
and **previews**: it never passes `confirm`, never wakes, never sends a command.
```bash
scripts/tesla_cli.sh daily_check --battery-threshold 30      # exit 1 if needs_attention is non-empty
scripts/tesla_cli.sh charge_check --threshold 50 --free-only # exit 1 if needs_charge
scripts/tesla_cli.sh preview set_temps --args '{"driver_temp_c": 21}'
scripts/tesla_cli.sh next_trip --arrival-time 2026-10-26T08:30 --destination "100 Main St, Springfield, IL"  # precondition sensible?
scripts/tesla_cli.sh preview add_precondition_schedule --args '{"time_hhmm":"07:30","days":["WEEKDAYS"],"latitude":42.28,"longitude":-83.74}'
```
`daily_check` remembers the newest alert time per car in `~/.local/state/tesla-mcp/alerts_last_seen.json`
(`--no-mark-seen` to leave it). The vehicle_data cache is per process, so separate CLI runs each cost $0.002 when the
car is online (use `--free-only` to avoid it).

## Offline / mock mode
`TESLA_MOCK=1 tesla-mcp` serves fake data for two cars (one online, one asleep) with no network or credentials.
Tests: `pytest -q`.

## Security notes
* Secrets are only read from env; tokens are stored in a 0600 file in a 0700 dir; errors are redacted.
* `keys/`, `*.pem`, `tokens.json`, `.env`, `proxy-config/` are git-ignored.
* Revoke the app's access any time: <https://auth.tesla.com/user/revoke/consent?revoke_client_id=YOUR_CLIENT_ID>.

## Sources (fetched 2026-10-23)
* Billing & limits: https://developer.tesla.com/docs/fleet-api/billing-and-limits
* Getting started / keys: https://developer.tesla.com/docs/fleet-api/getting-started/what-is-fleet-api
* Regions: https://developer.tesla.com/docs/fleet-api/getting-started/regions-countries
* Third-party tokens: https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens
* Partner tokens: https://developer.tesla.com/docs/fleet-api/authentication/partner-tokens
* Partner endpoints: https://developer.tesla.com/docs/fleet-api/endpoints/partner-endpoints
* Vehicle endpoints: https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-endpoints
* Vehicle commands: https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-commands
* Virtual keys: https://developer.tesla.com/docs/fleet-api/virtual-keys/developer-guide
* Fleet Telemetry fields: https://developer.tesla.com/docs/fleet-api/fleet-telemetry/available-data
* Signed-command proxy: https://github.com/teslamotors/vehicle-command
