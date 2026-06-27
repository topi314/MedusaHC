# MedusaHC Klipper extension

Klipper extras for a multi-hotend changer: Python handles sensors, state, validation, and UI sync; motion stays in cfg macros.

## Modules

| File | Klipper section | Role |
|------|-----------------|------|
| `scripts/medusahc.py` | `[medusahc]` | Orchestrator, `T0`–`Tn`/`DROP`, offsets, Mainsail tool buttons |
| `scripts/medusahc_calibrate.py` | `[medusahc_calibrate]` | Sexball kinematic probe calibration (optional) |

Sensor debouncing is built into `medusahc.py` via Klipper `[buttons]`.

## How it works

```
medusahc.py          reads switches, validates tool changes/DROP, exposes printer.medusahc
       │
       ▼  gcode macros
macros.cfg           internal `_…` motion macros; public `OPEN`/`CLOSE`/`T0`…`Tn`/`DROP`/`CLEAN`
```

- **Tool count** — from contiguous `[medusahc_tool 0]` … `[medusahc_tool N-1]`; no `max_tool`.
- **`T0`…`Tn`** — registered in Python as `gcode_macro T{n}` objects (`active` / `color` for Mainsail/Fluidd).
- **`OPEN` / `CLOSE`** — same pattern (Python commands exposed as `gcode_macro` objects for the macro UI).
- **Offsets** — G-code offsets relative to T0; stored in `[medusahc_tool N]` (`offset_x/y/z`).

Board hardware (`[extruder]`, `[servo]`, pin overrides) is **not** part of the extension bundle — keep that in your own `printer.cfg` includes.

---

## Install

One line (clones to `~/MedusaHC`, symlinks modules, installs config, registers Moonraker):

```bash
curl -fsSL https://raw.githubusercontent.com/topi314/MedusaHC/main/install.sh | bash -s -- --with-moonraker
```

```bash
wget -qO- https://raw.githubusercontent.com/topi314/MedusaHC/main/install.sh | bash -s -- --with-moonraker
```

Pass any `install.sh` flags after `bash -s --`. Override clone location or branch:

```bash
MEDUSAHC_REPO_BRANCH=main curl -fsSL https://raw.githubusercontent.com/topi314/MedusaHC/main/install.sh | bash -s -- --force
```

From a local clone:

```bash
git clone https://github.com/topi314/MedusaHC.git ~/MedusaHC
cd ~/MedusaHC
./install.sh --with-moonraker
```

Manual install:

```bash
ln -sf ~/MedusaHC/scripts/medusahc.py ~/klipper/klippy/extras/
ln -sf ~/MedusaHC/scripts/medusahc_calibrate.py ~/klipper/klippy/extras/
cp -r ~/MedusaHC/config/medusahc ~/printer_data/config/
```

### printer.cfg

```ini
# your board config (not shipped as part of the extension logic):
[include extruders.cfg]
[include servo.cfg]

# extension bundle:
[include medusahc/medusahc.cfg]
```

`install.sh` symlinks Python modules into `klippy/extras/` by default (use `--copy` to install copies instead).  
Flags: `--scripts-only`, `--config-only`, `--force`, `--copy`, `--with-moonraker`, `--with-eddy`, `--uninstall`.  
Config install is **non-destructive**: existing files in `medusahc/` are skipped; only missing files are added. Use `--force` to replace template files.  
Overrides: `KLIPPER_DIR`, `CONFIG_DIR`, `MOONRAKER_CONF`, `MEDUSAHC_REPO_DIR`, `MEDUSAHC_REPO_URL`, `MEDUSAHC_REPO_BRANCH`.

---

## Config bundle

```
config/medusahc/
├── medusahc.cfg    # [medusahc], [medusahc_tool N]
├── macros.cfg      # motion macros (edit freely)
└── calibrate.cfg   # optional [medusahc_calibrate] Sexball settings
```

### `[medusahc]`

| Option | Description |
|--------|-------------|
| `pin_e` | Head switch — tool seated on feeder |
| `init_delay` | Seconds before first sensor poll after ready |
| `assign_delay` | Debounce before recomputing tool state |
| `verbose` | Log pin transitions |
| `y_safe`, `y_latch`, `x_shift` | Pickup/drop geometry |
| `fast_accel`, `fast_speed` | Tool-change motion caps |
| `servo` | Feeder latch servo name (must match `[servo]` section) |
| `y_prime`, `y_brush`, `x_prime_shift` | Prime and brush positions |
| `x_clean_move` | Brush wipe stroke X amplitude in `_CLEAN_MOVE` (mm) |
| `e_open`, `e_close` | Feeder latch extruder distances (mm) |
| `e_run_current` | Optional extruder `run_current` override (A) if auto-detect fails |
| `e_cur_high_mult` | TMC current multiplier during `OPEN` / `CLOSE` |
| `eddy_tap_x/y/z`, `eddy_tap_f` | Bed tap point for eddy-ng Z calibration |
| `eddy_park_x/y` | Park position after `CALIBRATE_AND_SAVE_TOOL_Z_EDDY` |
| `sync_mainsail_tools` | Update `T{n}` `active` variable |
| `sync_mainsail_sensors` | Update `T{n}` `color` dock lamp |
| `color_active`, `color_pressed`, `color_released` | Lamp colours (hex, no `#`) |
| `tool_button_color` | Default tool button colour |

### `[medusahc_tool N]`

| Option | Required | Description |
|--------|----------|-------------|
| `dock_pin` | yes | Base dock switch |
| `x_base` | yes | Tool X position on base |
| `offset_x/y/z` | no | G-code offsets relative to T0 (updated by calibration; run `SAVE_CONFIG` to persist) |
| `prime_amount`, `prime_speed`, `prime_retract`, `prime_retract_speed` | no | Prime profile |
| `clean_move`, `clean_move_x/y`, `clean_move_speed`, `clean_retract*` | no | Brush profile |
| `first_prime_flag`, `first_prime_amount`, `first_prime_speed` | no | First prime after pickup |

Add or remove tools by adding/removing `[medusahc_tool N]` sections and matching `[extruderN]` in your board config.

### `macros.cfg`

Macros called by Python — safe to edit without restarting logic:

| Macro | Used by |
|-------|---------|
| `_OPEN_START`, `_OPEN_MOVE` | `OPEN` |
| `_CLOSE_MOVE` | `CLOSE` |
| `_SET_MOVE`, `_POST_PICKUP` | `T0`…`Tn` |
| `_DROP_MOVE` | `DROP` |
| `_CLEAN_MOVE` | `CLEAN` |
| `_MEDUSAHC_ERROR_PAUSE` | `ERROR` |

`_POST_PICKUP` calls `_SET_FIRST_PRIME_FLAG` (internal). Macros call `TOOL_OFFSET_T`, `OPEN`, and `CLOSE` (Python commands).

`ERROR` calls `_MEDUSAHC_ERROR_PAUSE`, which lifts Z, moves to `y_safe`, then `_PAUSE` (Klipper’s built-in pause — **not** your `PAUSE` macro). Your job `PAUSE` macro should use `rename_existing: _PAUSE` (Mainsail/KAMP default). On cancel after a tool-change error, guard `PRINT_END` with `printer.medusahc.error` so it does not run `DROP_TOOL` / `PARK` / `RESTORE_GCODE_STATE`.

### `[medusahc_calibrate]`

Optional. Include `calibrate.cfg` in `medusahc.cfg` when Sexball hardware is present. Probe pin, station XY/Z, spread, speeds — see `config/medusahc/calibrate.cfg`.

---

## Commands

### `medusahc`

User-facing macros (shown in Mainsail/Fluidd): `OPEN`, `CLOSE`, `CLEAN`, `CALIBRATE_AND_SAVE_TOOL_Z_EDDY`.

| Command | Description |
|---------|-------------|
| `DROP` | Drop current tool |
| `DROP_CLOSE` | Drop and close feeder |
| `DROP_TOOL` | Drop without close sequence |
| `OPEN` / `CLOSE` | Feeder latch |
| `CLEAN` | Brush routine |
| `CALIBRATE_AND_SAVE_TOOL_Z_EDDY` | Eddy-ng tap Z for all tools, save, park |
| `TOOL_OFFSET_T T=n MOVE=0\|1` | Apply offsets for tool *n* |
| `SET_TOOL_Z_OFFSET VALUE=z` | Record probe Z (internal; used by eddy-ng tap) |
| `LAYER_SET L=n` | Set layer counter |
| `PRIME_FLAGS_SET` / `PRIME_FLAGS_CLEAR` | Reset first-prime flags |
| `CLEAR_ERROR` | Clear error state |
| `ERROR` | Pause print at dock (`y_safe`) on tool-change failure |
| `T0` … `Tn` | Pick up tool *n* |

`INIT_SENSOR_STATE` runs on `klippy:ready` — logs `[medusahc_tool N]` offsets, reads extruder TMC current, closes feeder.

### `medusahc_calibrate`

User-facing macro (shown in Mainsail/Fluidd):

| Command | Description |
|---------|-------------|
| `CALIBRATE_AND_SAVE_OFFSETS` | Full Sexball XY/Z calibration, save, park |

---

## `printer.medusahc` status

Moonraker object: `medusahc`. Jinja: `{% set m = printer.medusahc %}`.

### Global

| Key | Type | Meaning |
|-----|------|---------|
| `state` | str | `uninitialized`, `ready`, `changing`, `error` |
| `current_tool` | int | `-2` fault, `-1` empty, `0…N-1` on head |
| `target_tool` | int | Last `T{n}` selection target |
| `tool_count` | int | Number of tools |
| `head_loaded` | bool | `pin_e` triggered |
| `feeder_open` | bool | Latch open |
| `error` | bool | Error latched |
| `layer` | int | From `LAYER_SET` |
| `eddy_z`, `t0_probe_z` | float | Eddy calibration helpers |
| `e_cur`, `e_cur_high` | float | Extruder TMC currents |
| `servo` | str | Feeder latch servo name |
| `y_safe`, `y_latch`, `x_shift`, `x_clean_move`, … | float | Motion defaults from `[medusahc]` |

### Per tool (`N` = 0 … tool_count−1)

| Key | Meaning |
|-----|---------|
| `toolN_docked` | Dock switch |
| `toolN_x_base` | Base X |
| `toolN_offset_x/y/z` | Active G-code offsets |
| `toolN_prime_*` | Prime profile |
| `toolN_clean_*` | Clean profile |
| `toolN_first_prime_*` | First-prime profile |

### `printer.medusahc_calibrate`

| Key | Meaning |
|-----|---------|
| `last_x_result`, `last_y_result`, `last_z_result` | Last calibration result |
| `last_probe_offset` | Nozzle-probe Z offset |
| `calibration_probe_inactive` | Probe endstop state |

---

## Calibration notes

Offsets are **G-code offsets** relative to T0. Test-print tool offsets use the **opposite sign**.

After calibration with save enabled, run **`SAVE_CONFIG`** to write `#*# [medusahc_tool N]` overrides to `printer.cfg` and restart Klipper.

XY and Z calibration are independent — use either or both:

**Sexball (XY + relative Z):** include `calibrate.cfg`, configure `[medusahc_calibrate]`, run `CALIBRATE_AND_SAVE_OFFSETS`.

**Eddy-ng tap (Z only):** install eddy-ng (`./install.sh --with-eddy`), set `eddy_tap_x/y` in `[medusahc]`, home Z, then run `CALIBRATE_AND_SAVE_TOOL_Z_EDDY` from the macro panel. Does not require Sexball or `[medusahc_calibrate]`.

---

## Moonraker

```ini
[update_manager medusahc]
type: git_repo
path: ~/MedusaHC
origin: https://github.com/topi314/MedusaHC.git
primary_branch: main
is_system_service: False
managed_services: klipper
```

Or: `./install.sh --with-moonraker`.

Updates pull Python via symlink and do not touch your `printer_data/config/medusahc/` files. Merge template changes from the repo manually when needed.

---

## Example

Full 4-tool example: `config/medusahc/medusahc.cfg` (trim `[medusahc_tool N]` sections for fewer tools).

## License

GPL-3.0 — see [LICENSE](LICENSE).
