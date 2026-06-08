# MedusaHC Klipper extension

Klipper extras for a multi-hotend changer: Python handles sensors, state, validation, and UI sync; motion stays in cfg macros.

## Modules

| File | Klipper section | Role |
|------|-----------------|------|
| `Scripts/medusahc.py` | `[medusahc]` | Orchestrator, `SET`/`DROP`, offsets, Mainsail tool buttons |
| `Scripts/medusahc_calibrate.py` | `[medusahc_calibrate]` | Sexball kinematic probe calibration (optional) |

No `klipper-toolchanger`, no `pin_watch.py`. Sensor debouncing is built into `medusahc.py` via `[buttons]`.

## How it works

```
medusahc.py          reads switches, validates SET/DROP, exposes printer.medusahc
       │
       ▼  gcode macros
motion.cfg           OPEN/CLOSE, SET_MOVE, DROP_MOVE, POST_PICKUP, CLEAN_MOVE
```

- **Tool count** — from contiguous `[medusahc_tool 0]` … `[medusahc_tool N-1]`; no `max_tool`.
- **`T0`…`Tn`** — registered in Python as `gcode_macro T{n}` objects (`active` / `color` for Mainsail/Fluidd).
- **Offsets** — G-code offsets relative to T0; persisted in `medusahc/saved_vars.cfg`.

Board hardware (`[extruder]`, `[servo]`, pin overrides) is **not** part of the extension bundle — keep that in your own `printer.cfg` includes.

---

## Install

One line (clones to `~/MedusaHC`, symlinks modules, installs config, registers Moonraker):

```bash
curl -fsSL https://raw.githubusercontent.com/Irbis3D/MedusaHC/main/install.sh | bash -s -- --with-moonraker
```

```bash
wget -qO- https://raw.githubusercontent.com/Irbis3D/MedusaHC/main/install.sh | bash -s -- --with-moonraker
```

Pass any `install.sh` flags after `bash -s --`. Override clone location or branch:

```bash
MEDUSAHC_REPO_BRANCH=main curl -fsSL https://raw.githubusercontent.com/Irbis3D/MedusaHC/main/install.sh | bash -s -- --force
```

From a local clone:

```bash
git clone https://github.com/Irbis3D/MedusaHC.git ~/MedusaHC
cd ~/MedusaHC
./install.sh --with-moonraker
```

Manual install:

```bash
ln -sf ~/MedusaHC/Scripts/medusahc.py ~/klipper/klippy/extras/
ln -sf ~/MedusaHC/Scripts/medusahc_calibrate.py ~/klipper/klippy/extras/
cp -r ~/MedusaHC/Config/medusahc ~/printer_data/config/
```

### printer.cfg

```ini
# your board config (not shipped as part of the extension logic):
[include extruders.cfg]
[include servo.cfg]

# extension bundle:
[include medusahc/medusahc.cfg]
```

`install.sh` flags: `--scripts-only`, `--config-only`, `--force`, `--symlink`, `--with-moonraker`, `--with-eddy`, `--uninstall`.  
Overrides: `KLIPPER_DIR`, `CONFIG_DIR`, `MOONRAKER_CONF`, `MEDUSAHC_REPO_DIR`, `MEDUSAHC_REPO_URL`, `MEDUSAHC_REPO_BRANCH`.

---

## Config bundle

```
medusahc/
├── medusahc.cfg           # [medusahc], [medusahc_tool N], [save_variables]
├── motion.cfg             # motion macros (edit freely)
├── calibrate-offsets.cfg  # [medusahc_calibrate] probe settings
└── saved_vars.cfg         # offset persistence
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
| `y_prime`, `y_brush`, `x_prime_shift` | Prime and brush positions |
| `e_open`, `e_close` | Feeder latch extruder distances (mm) |
| `e_cur_high_mult` | TMC current multiplier during `OPEN` |
| `sync_mainsail_tools` | Update `T{n}` `active` variable |
| `sync_mainsail_sensors` | Update `T{n}` `color` dock lamp |
| `color_active`, `color_pressed`, `color_released` | Lamp colours (hex, no `#`) |
| `tool_button_color` | Default tool button colour |

### `[medusahc_tool N]`

| Option | Required | Description |
|--------|----------|-------------|
| `dock_pin` | yes | Base dock switch |
| `x_base` | yes | Tool X position on base |
| `offset_x/y/z` | no | Initial G-code offsets |
| `prime_amount`, `prime_speed`, `prime_retract`, `prime_retract_speed` | no | Prime profile |
| `clean_move`, `clean_move_x/y`, `clean_move_speed`, `clean_retract*` | no | Brush profile |
| `first_prime_flag`, `first_prime_amount`, `first_prime_speed` | no | First prime after pickup |

Add or remove tools by adding/removing `[medusahc_tool N]` sections and matching `[extruderN]` in your board config.

### `motion.cfg`

Macros called by Python — safe to edit without restarting logic:

| Macro | Used by |
|-------|---------|
| `OPEN_START`, `OPEN_MOVE` | `OPEN` |
| `CLOSE_MOVE` | `CLOSE` |
| `SET_MOVE`, `POST_PICKUP` | `SET` |
| `DROP_MOVE` | `DROP` |
| `CLEAN_MOVE` | `CLEAN` |

### `[medusahc_calibrate]`

Probe pin, station XY/Z, spread, speeds — see `calibrate-offsets.cfg`. Requires a kinematic probe (Sexball) wired per section `pin`.

### `[save_variables]`

In `medusahc.cfg`:

```ini
[save_variables]
filename: medusahc/saved_vars.cfg
```

Variables written: `tN_gcode_x_offset`, `tN_gcode_y_offset`, `tN_gcode_z_offset`.

---

## Commands

### `medusahc`

| Command | Description |
|---------|-------------|
| `SET T=n` | Pick up tool *n* |
| `DROP` | Drop current tool |
| `DROP_CLOSE` | Drop and close feeder |
| `DROP_TOOL` | Drop without close sequence |
| `OPEN` / `CLOSE` | Feeder latch |
| `CLEAN` | Brush routine |
| `TOOL_OFFSET_T T=n MOVE=0\|1` | Apply offsets for tool *n* |
| `SET_TOOL_Z_OFFSET VALUE=z` | Record probe Z (Eddy multi-tool) |
| `LAYER_SET L=n` | Set layer counter |
| `PRIME_FLAGS_SET` / `PRIME_FLAGS_CLEAR` | Reset first-prime flags |
| `MHC_CLEAR_ERROR` | Clear error state |
| `T0` … `Tn` | Select tool (= `SET T=n`) |

`INIT_SENSOR_STATE` runs on `klippy:ready` — loads saved offsets, reads extruder TMC current, closes feeder.

### `medusahc_calibrate`

| Command | Description |
|---------|-------------|
| `CALIBRATE_AND_SAVE_OFFSETS` | Full calibration, save, park |
| `CALIBRATE_TOOL_OFFSETS` | Calibrate tools 1…N (`TOOLS=`, `SAVE=`, `DROP=`) |
| `CALIBRATE_MOVE_OVER_PROBE` | Move to probe station |
| `CALIBRATE_NOZZLE_PROBE_OFFSET` | Nozzle vs probe Z (`TEMP=`) |
| `SAVE_TOOL_GCODE_OFFSETS T=n` | Write current offsets to `saved_vars` |

---

## `printer.medusahc` status

Moonraker object: `medusahc`. Jinja: `{% set m = printer.medusahc %}`.

### Global

| Key | Type | Meaning |
|-----|------|---------|
| `state` | str | `uninitialized`, `ready`, `changing`, `error` |
| `current_tool` | int | `-2` fault, `-1` empty, `0…N-1` on head |
| `target_tool` | int | Last `SET` target |
| `tool_count` | int | Number of tools |
| `head_loaded` | bool | `pin_e` triggered |
| `feeder_open` | bool | Latch open |
| `error` | bool | Error latched |
| `layer` | int | From `LAYER_SET` |
| `eddy_z`, `t0_probe_z` | float | Eddy calibration helpers |
| `e_cur`, `e_cur_high` | float | Extruder TMC currents |
| `y_safe`, `y_latch`, `x_shift`, … | float | Motion defaults from `[medusahc]` |

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

**Sexball:** configure `[medusahc_calibrate]`, run `CALIBRATE_AND_SAVE_OFFSETS`.

**Eddy-ng (optional):** deploy `Scripts/probe_eddy_ng.py` to `~/eddy-ng`, include `eddy_ng_features.cfg`, run `TOOL_Z_CALIBRATION`.

---

## Moonraker

```ini
[update_manager medusahc]
type: git_repo
path: ~/MedusaHC
origin: https://github.com/Irbis3D/MedusaHC.git
primary_branch: main
system_dependencies: system-dependencies.json
is_system_service: False
managed_services: klipper
```

Or: `./install.sh --with-moonraker`.

Updates pull Python via symlink; re-run `./install.sh --config-only` when cfg templates change.

---

## Migrate from legacy MHC macros

1. Remove `[include MHC_variables.cfg]`, `MHC_macros.cfg`, `toolchanger.cfg`
2. Remove `klippy/extras/pin_watch.py`
3. Replace with `[include medusahc/medusahc.cfg]`
4. Move offsets file to `medusahc/saved_vars.cfg`

---

## Example

Minimal `[medusahc]` + two tools: `Example-Config/medusahc.cfg`

## License

GPL-3.0 — see [LICENSE](LICENSE).
