# nitro-fan — fan and power control for Acer Nitro AN515-55 on Linux

![License](https://img.shields.io/badge/license-MIT-green)
![Linux kernel](https://img.shields.io/badge/kernel-6.x%20%2F%207.x-orange)
![GUI](https://img.shields.io/badge/GUI-GTK3%20%2B%20tray-blue)
![EC layout](https://img.shields.io/badge/EC-layout%20verified-success)

**English | [Русский](README.md)**

**nitro-fan** is a toolkit that brings back quiet, sensible cooling control for
the **Acer Nitro 5 (AN515-55)** laptop on Linux:

- **nitro-fan.sh** — CLI: CPU modes (quiet / balanced / full power), fan speed
  fixing, EC diagnostics (register reads, live tests), systemd unit templates;
- **nbfc-quiet-curve.json** — quiet fan-curve config for
  [NBFC-Linux](https://github.com/nbfc-linux/nbfc-linux), based on an EC layout
  verified live on this machine;
- **nitro-gui.py** — a GTK3 app: status (temperatures, RPM, EPP), mode
  switching, manual fan speed fixing, a graphical curve editor, tray icon.

## Table of contents

1. [How the motherboard controls the fans](#1-how-the-motherboard-controls-the-fans)
2. [What Linux currently sees (facts from this machine)](#2-what-linux-currently-sees-facts-from-this-machine)
3. [The quiet setup and why](#3-the-quiet-setup-and-why)
4. [The nitro-gui.py GUI app](#4-the-nitro-guipy-gui-app)
5. [The nitro-fan.sh script](#5-the-nitro-fansh-script)
6. [NBFC-Linux — installed and working](#6-nbfc-linux--installed-and-working)
7. [Safety notes](#7-safety-notes)
8. [Sources](#8-sources)

## Quick start

```bash
git clone git@github.com:rsmagulov/nitro-fan.git && cd nitro-fan
./nitro-fan.sh status              # temps, fans, EPP, freq cap
sudo ./nitro-fan.sh cpu-quiet on   # quiet CPU mode in one command
python3 nitro-gui.py               # GUI
```

NBFC-Linux: install as described in section [6](#6-nbfc-linux--installed-and-working)
(or run `sudo ./nitro-fan.sh install-nbfc`).

## License

Released under the [MIT](LICENSE) license.

---

## 1. How the motherboard controls the fans

On the Nitro 5 the cooling is driven entirely by the Embedded Controller (EC,
"the motherboard's built-in microcontroller"— Stonic_CMS chip, BIOS V2.06).
Linux does not talk to it directly: the firmware itself watches CPU/GPU/NVMe
temperatures and spins the two fans along the curves baked into it. Known EC
registers for this generation (confirmed against community configs for
AN515-44/AN515-54):

| Purpose                     | EC address |
|-----------------------------|------------|
| CPU fan: RPM read / write   | 0x13 / 0x37 |
| GPU fan: RPM read / write   | 0x15 / 0x3A |
| CPU fan manual mode         | 0x22 = 0x0C (re-written before every speed write) |
| GPU fan manual mode         | 0x21 = 0x30 (re-written before every speed write) |
| Global manual mode          | 0x97 = 0x01 |
| CoolBoost (NitroSense overdrive) | 0x16 (0 = off) |

Key quirk: the EC periodically drops "manual mode", so writes must be repeated
periodically (that's what the NBFC service / our script does).

## 2. What Linux currently sees (facts from this machine)

- hwmon: **not a single** `pwm*`/`fan*` interface → `fancontrol`/`pwmconfig`
  are useless. Fan RPM is not even visible.
- Sensors: coretemp (package up to 77 °C under load), acpitz 61 °C, PCH 49 °C,
  NVMe 32 °C, iwlwifi 39 °C; GPU 53–55 °C via nvidia-smi.
- thermald is active (protective CPU throttling), governor
  intel_pstate=powersave. power-profiles-daemon: the user picked the
  "Balanced" profile; the `cpu-quiet` overlay runs on top of it
  (EPP=balance_power + 2600 MHz cap).
- `/sys/firmware/acpi/platform_profile` does not exist — the stock `acer_wmi`
  module does not register it for this laptop (profiles are provided by
  Linuwu-Sense, see below).

## 3. The quiet setup and why

A two-level approach:

**A. Works right away (reduces noise without any EC writes)**

- `balanced` profile instead of `performance`;
- EPP `balance_power` instead of `performance`;
- CPU frequency cap 2600 MHz (instead of 5000). Peak current of the
  i7-10750H's 6 cores is the main noise source; capping it cools things down
  noticeably in day-to-day use.
- All of this is done by `sudo ./nitro-fan.sh cpu-quiet on` (reversible, state
  is saved). Fans stay under EC firmware's stock curves — safe. `on` also
  enables the unit's boot-time autostart; `off` disables it, so "full power"
  (`powerprofilesctl set performance` + `cpu-quiet off`) survives a reboot.

**B. Fan control (NBFC-Linux)** — the quiet two-stage curve (recommended
values):

| | CPU fan | GPU fan |
|---|---|---|
| spin-up | 55°C → 20% | 50°C → 20% |
| quiet cruise | 60–65°C → 25–32% | 55–60°C → 25–30% |
| normal | 70–75°C → 40–55% | 65–70°C → 40–55% |
| boost | 80°C → 72%, 85°C → 90% | 75°C → 72%, 80°C → 90% |
| max | 90°C → 100% | 85°C → 100% |
| emergency (script) | ≥87°C → 100% | ≥83°C → 100% |

These are softer than NBFC's stock curve (which starts at 12% already at
42 °C — on this model the fans are always audible) and noticeably quieter
than the firmware curve (the Nitro keeps comparatively high RPM from
~45–50 °C), while still protecting against overheating: thermald keeps
guarding the CPU, and the script forces 100% at emergency thresholds.

## 4. The nitro-gui.py GUI app

GTK3 (python-gi is preinstalled). Launch it from the applications menu
("Nitro: fans and power" — the .desktop file and icon are installed into
~/.local/share on first run) or via `python3 ~/nitro-fan/nitro-gui.py`.
Closing the window hides it to the tray (icon with a quick-mode menu).

- **Status** — CPU/GPU temperatures and RPM, PPD profile, EPP, frequency cap,
  selected NBFC config; refreshes every 2 s.
- **Modes** — three buttons: Quiet / Native balanced / Full power. Quiet and
  Full power use pkexec (a polkit dialog); speed fixing needs no password
  (NBFC socket is 0666).
- **Fans** — speed fixing (`nbfc set -s`), return to auto, a curve editor
  with 9 thresholds 50–90 °C × 2 fans plus "Quiet"/"Dynamic" presets;
  "Apply curve" builds the "Acer Nitro AN515-55 Custom" config and installs
  it with a single pkexec. Curve settings are stored in
  ~/.config/nitro-fan/curve.json.

## 5. The nitro-fan.sh script

```
./nitro-fan.sh status                 # temps, profile, EPP, freq cap
./nitro-fan.sh watch [sec]            # the same in a loop
sudo ./nitro-fan.sh cpu-quiet on      # quiet CPU mode (works out of the box)
sudo ./nitro-fan.sh cpu-quiet off     # revert
sudo ./nitro-fan.sh cpu-quiet-apply 2600000   # same, without saving state (for autostart)
./nitro-fan.sh fan-speed 30           # set fan speed (NBFC required)
./nitro-fan.sh fan-auto               # return to the NBFC profile
./nitro-fan.sh fan-curve              # quiet daemon: emergency → 100%, restores auto on exit
./nitro-fan.sh install-nbfc           # step-by-step NBFC-Linux installation
./nitro-fan.sh unit                   # systemd unit for fan-curve autostart
./nitro-fan.sh unit-cpuquiet          # systemd unit for cpu-quiet autostart
./nitro-fan.sh ec-prep / ec-read / ec-test [sec] / ec-max / ec-sweep
```

**Autostart (installed):** `nitro-cpu-quiet.service` — a oneshot unit;
the script copy lives at `/usr/local/bin/nitro-fan`; it runs after
power-profiles-daemon and applies EPP=balance_power + the 2600 MHz cap.
Check/control: `systemctl status/restart/disable nitro-cpu-quiet`.

## 6. NBFC-Linux — installed and working

nbfc-linux 0.5.4 is installed (deb for Mint 22 — in ~/Downloads).

- Config: "Acer Nitro AN515-55 Quiet" (the json copy is nbfc-quiet-curve.json).
- The GPU fan sensor is pinned to the max over CPU cores
  (`nbfc sensors set -f 1 -s coretemp -a Max`): the NVML source
  (`nvidia-ml`) is not picked up by this build.
- Service: `nbfc_service.service` — enabled + active (write mode); ec_sys is
  loaded with the module rules:
  /etc/modprobe.d/ec_sys.conf (`options ec_sys write_support=1`) and
  /etc/modules-load.d/ec_sys.conf (`ec_sys`).

```
nbfc status -a        # RPM/targets (nbfc status -a -w 3 — live monitor)
nbfc set -s 40        # both fans to 40%
nbfc set --auto       # return to the profile curve
sudo systemctl start/stop/restart nbfc_service
```

Live check (Nov 17): at idle NBFC writes CPU 0 → the EC releases and holds
its firmware floor (~2000 RPM), GPU ~20-25% — also the firmware floor
(~2300). The noise is the same as stock, but when the laptop heats up the
curve follows ours: 55°→20%, 60–65°→25–32%, 70–75°→40–55%, 80°→72%,
85°→90%, 90°→100%.

Caveat: while NBFC is in write mode, do NOT run the script's `ec-*` commands —
NBFC will re-assert the manual bits.

The config file is based on the verified EC layout of this Nitro 5 generation
(AN515-54: CPU 0x13/0x37, GPU 0x15/0x3A, manual modes 0x22/0x21/0x97,
CoolBoost 0x16). There is no official NBFC config for the AN515-55 — but the
AN515-44/-54 (same Comet Lake-H platform) match historically.

NBFC alternative: the kernel module
[Linuwu-Sense](https://github.com/0x7375646F/Linuwu-Sense) (DKMS, replaces
`acer_wmi`): adds `fan_speed`, battery charge limiters, RGB and
`/sys/firmware/acpi/platform_profile` (quiet/balanced/performance) for the
Nitro. Install per its README; Nimbus interface example:
`echo 50,70 | sudo tee .../nitro_sense/fan_speed`.

## 7. Safety notes

- `fan-curve` catches SIGINT/TERM/EXIT and returns the fans to `--auto`.

### 7.1 Live test results on this device (Nov 17, BIOS V2.06)

- `ec-read`/`ec-test`/`ec-max`/`ec-sweep` (nitro-fan.sh): the AN515-55 EC
  layout matches AN515-44/-54 exactly; the manual bits latch (12/48/1) and
  duty at 0x37/0x3A works (100% → CPU RPM 2013→5172, GPU 2325→6122 — exactly
  MaxSpeedValueRead=6122 from the config).
- **Full stop is impossible**: duty=0 is treated by the firmware as
  "release → auto" (hence FanSpeedResetValue=0 in NBFC configs), and small
  duties (≤~40) are pulled up by the EC to its firmware minimum of
  ~2000–2350 RPM — the same "quiet" level as in auto mode. The AN515-55 fans
  simply cannot be stopped by the firmware (consistent with owner reports:
  neither NBFC nor Linuwu-Sense can fully silence this generation).
- After the ec-* tests `ec_sys` stays loaded (write_support=1); only root can
  write (io 0600). To unload: `sudo rmmod ec_sys`.
- EC writes are repeated only by the NBFC service; there are no raw manual
  pokes left behind.
- If fan control gets stuck after failed experiments (fans never return):
  a full power-off (unplug the charger, remove the main battery for 2–3
  minutes) resets the EC.
- thermald and the firmware sensors keep working on top of all of this.

## 8. Sources

- NBFC-Linux README and nbfc man page: https://github.com/nbfc-linux/nbfc-linux
- Nitro EC configs: `Acer Nitro AN515-44.json` (inside the NBFC repo) and
  https://github.com/Kaspral1/Acer-Nitro-Perfect-Fan (AN515-54)
- Linuwu-Sense: https://github.com/0x7375646F/Linuwu-Sense
