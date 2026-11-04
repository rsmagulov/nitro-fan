#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
    HAS_TRAY = True
except (ValueError, ImportError):
    AppIndicator = None
    HAS_TRAY = False

APP_TITLE = "Nitro: вентиляторы и мощность"
ICON_NAME = "nitro-fans"

DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_SYS = "/usr/local/bin/nitro-fan"
SCRIPT_LOCAL = os.path.join(DIR, "nitro-fan.sh")
QUIET_JSON = os.path.join(DIR, "nbfc-quiet-curve.json")
CURVE_DIR = os.path.expanduser("~/.config/nitro-fan")
CURVE_STATE = os.path.join(CURVE_DIR, "curve.json")
CUSTOM_GEN = os.path.join(CURVE_DIR, "nbfc-custom.json")
CUSTOM_NAME = "Acer Nitro AN515-55 Custom"
QUIET_NAME = "Acer Nitro AN515-55 Quiet"
NBFC_ETC = "/etc/nbfc/nbfc.json"
ACTIVE_MARKER = os.path.expanduser("~/.cache/nitro-fan/cpu-quiet.active")
UNIT_NAME = "nitro-cpu-quiet.service"
AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "nitro-fans.desktop")
VERSION_FILE = os.path.join(DIR, "VERSION")

THRESHOLDS = [50, 55, 60, 65, 70, 75, 80, 85, 90]
BASE_CURVE = {"cpu": [0, 20, 25, 32, 40, 55, 72, 90, 100],
              "gpu": [0, 20, 25, 30, 40, 55, 72, 90, 100]}
SPORT_CURVE = {"cpu": [12, 28, 36, 45, 55, 65, 80, 90, 100],
               "gpu": [12, 28, 36, 45, 55, 65, 80, 90, 100]}

MODE_LABELS = {
    "quiet": "Тихий",
    "balanced": "Родной баланс",
    "performance": "Полная мощность",
}
PROF_RU = {
    "balanced": "Баланс (power-profiles-daemon)",
    "performance": "Полная мощность (power-profiles-daemon)",
    "power-saver": "Энергосбережение (power-profiles-daemon)",
}
EPP_RU = {
    "performance": "производительность (максимум скорости, больше нагрев)",
    "balance_performance": "баланс с наклоном к производительности",
    "balance_power": "баланс с наклоном к энергосбережению",
    "power": "энергосбережение (самый тихий)",
}

CSS = b"""
button.mode { font-size: 16px; padding: 15px 12px; }
button.mode.active { background: rgba(70, 160, 240, 0.35); }
frame.block { border: 1px solid alpha(@theme_fg_color, 0.18); border-radius: 12px;
              background: alpha(@theme_fg_color, 0.045); }
frame.block > label { padding: 2px 8px; font-weight: bold; }
label.key { color: alpha(@fg_color, 0.62); }
label.temp { font-size: 36px; font-weight: 600; }
label.temp.warm { color: #d98f1d; }
label.temp.hot { color: #e04545; }
label.muted { color: alpha(@fg_color, 0.6); font-size: 12px; }
progressbar { min-height: 13px; }
progressbar trough { border-radius: 4px; }
progressbar progress { border-radius: 4px; }
progressbar.calm trough progress { background: #5fa86b; }
progressbar.warn trough progress { background: #d2a83f; }
progressbar.hot trough progress { background: #c85e5e; }
stack { padding: 2px; }
"""


def run(cmd, timeout=20):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.returncode, out.stdout, out.stderr
    except FileNotFoundError:
        return 127, "", "нет команды: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "таймаут"
    except Exception as e:
        return 1, "", str(e)


def pk_script(script):
    home = os.path.expanduser("~")
    disp = os.environ.get("DISPLAY", ":0")
    return run(["pkexec", "env", "DISPLAY=%s" % disp, "HOME=%s" % home,
                "XAUTHORITY=%s" % os.path.join(home, ".Xauthority"),
                "bash", "-c", script])


def coretemp_hwmon():
    for d in sorted(os.listdir("/sys/class/hwmon")):
        try:
            with open(os.path.join("/sys/class/hwmon", d, "name")) as f:
                if f.read().strip() == "coretemp":
                    return os.path.join("/sys/class/hwmon", d)
        except OSError:
            continue
    return None


def read_temp_cpu():
    base = coretemp_hwmon()
    if not base:
        return None
    try:
        with open(os.path.join(base, "temp1_input")) as f:
            return int(f.read()) / 1000.0
    except (OSError, ValueError):
        return None


def read_temp_gpu():
    if not os.path.exists("/proc/driver/nvidia/gpus"):
        return None
    rc, out, _ = run(["nvidia-smi", "--query-gpu=temperature.gpu",
                      "--format=csv,noheader,nounits"])
    if rc == 0:
        try:
            return float(out.splitlines()[0])
        except (ValueError, IndexError):
            return None
    return None


def read_epp():
    try:
        with open("/sys/devices/system/cpu/cpufreq/policy0/"
                  "energy_performance_preference") as f:
            return f.read().strip()
    except OSError:
        return None


def read_maxfreq_mhz():
    try:
        with open("/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq") as f:
            return int(round(int(f.read()) / 1000))
    except (OSError, ValueError):
        return None


def read_profile():
    rc, out, _ = run(["powerprofilesctl", "get"])
    return out.strip() if rc == 0 and out.strip() else None


def read_active_marker():
    try:
        with open(ACTIVE_MARKER) as f:
            return f.read().strip() == "on"
    except OSError:
        return False


def read_unit_enabled():
    rc, out, _ = run(["systemctl", "is-enabled", UNIT_NAME])
    return rc == 0 and out.strip() == "enabled"


def read_nbfc_selected():
    try:
        with open(NBFC_ETC) as f:
            return json.load(f).get("SelectedConfigName")
    except (OSError, ValueError):
        return None


def read_fans():
    rc, out, _ = run(["nbfc", "status", "-a"])
    fans = []
    if rc == 0:
        for chunk in out.split("Fan Display Name")[1:]:
            def fld(key):
                m = re.search(r"%s\s*:\s*([\d.]+)" % key, chunk)
                return float(m.group(1)) if m else None
            name = chunk.splitlines()[0].strip().lstrip(":").strip()
            temp = fld("Temperature")
            cur = fld("Current Fan Speed")
            tgt = fld("Target Fan Speed")
            steps = fld("Fan Speed Steps")
            if name and None not in (temp, cur, tgt, steps):
                fans.append({"name": name, "temp": temp, "cur": cur,
                             "tgt": tgt, "steps": int(steps)})
    return fans


def fan_zone(f):
    t, c = f.get("temp") or 0, f.get("cur") or 0
    if t >= 85 or c >= 90:
        return "hot"
    if t >= 70 or c >= 60:
        return "warn"
    return "calm"


def read_status():
    return {"cpu": read_temp_cpu(), "gpu": read_temp_gpu(),
            "epp": read_epp(), "max": read_maxfreq_mhz(),
            "profile": read_profile(), "quiet_active": read_active_marker(),
            "unit": read_unit_enabled(),
            "nbfc_cfg": read_nbfc_selected(), "fans": read_fans()}


def detect_mode(d):
    if d.get("quiet_active"):
        return "quiet"
    profile = d.get("profile") or ""
    if profile == "performance":
        return "performance"
    return "balanced"


def load_curve():
    try:
        with open(CURVE_STATE) as f:
            data = json.load(f)
        cpu = [int(v) for v in data["cpu"]]
        gpu = [int(v) for v in data["gpu"]]
        if len(cpu) == len(THRESHOLDS) and len(gpu) == len(THRESHOLDS):
            return cpu, gpu
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return list(BASE_CURVE["cpu"]), list(BASE_CURVE["gpu"])


def save_curve(cpu_vals, gpu_vals):
    os.makedirs(CURVE_DIR, exist_ok=True)
    with open(CURVE_STATE, "w") as f:
        json.dump({"cpu": cpu_vals, "gpu": gpu_vals}, f, indent=2)


def build_custom_json(cpu_vals, gpu_vals):
    with open(QUIET_JSON) as f:
        cfg = json.load(f)
    cfg["NotebookModel"] = CUSTOM_NAME
    cfg["Author"] = "nitro-gui"
    for fan, vals in zip(cfg["FanConfigurations"], (cpu_vals, gpu_vals)):
        th, prev = [], 0
        for idx, (t, sp) in enumerate(zip(THRESHOLDS, vals)):
            down = prev if idx > 0 else 0
            th.append({"UpThreshold": t, "DownThreshold": down, "FanSpeed": sp})
            prev = t
        fan["TemperatureThresholds"] = th
    os.makedirs(CURVE_DIR, exist_ok=True)
    with open(CUSTOM_GEN, "w") as f:
        json.dump(cfg, f, indent=2)


def apply_custom_curve(cpu_vals, gpu_vals):
    build_custom_json(cpu_vals, gpu_vals)
    script = ('install -D -m 644 "{s}" "/var/lib/nbfc/configs/{n}.json" && '
              'nbfc config --set "{n}" && systemctl restart nbfc_service'
              ).format(s=CUSTOM_GEN, n=CUSTOM_NAME)
    return pk_script(script)


def reset_curve():
    script = ('nbfc config --set "{n}" && systemctl restart nbfc_service'
              ).format(n=QUIET_NAME)
    return pk_script(script)


def pkexec_cpu_quiet(arg):
    script = ('"{s}" cpu-quiet {a}').format(
        s=SCRIPT_SYS if os.path.exists(SCRIPT_SYS) else SCRIPT_LOCAL, a=arg)
    return pk_script(script)


def mode_action(mode):
    profile = "performance" if mode == "performance" else "balanced"
    r1 = run(["powerprofilesctl", "set", profile])
    r2 = pkexec_cpu_quiet("on" if mode == "quiet" else "off")
    return r1, r2


SVG = """<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'
 viewBox='0 0 64 64'><circle cx='32' cy='32' r='29' fill='#333'/>
 <g fill='#66b2ff'><path d='M32 8a10 10 0 0 1 8 16c-3 4-8 5-11 2 1-6 1-12 3-18z'/>
 <path d='M55 32a10 10 0 0 1-16 8c-4-3-5-8-2-11 6 1 12 1 18 3z'/>
 <path d='M32 55a10 10 0 0 1-8-16c3-4 8-5 11-2-1 6-1 12-3 18z'/>
 <path d='M9 32a10 10 0 0 1 16-8c4 3 5 8 2 11-6-1-12-1-18-3z'/></g>
 <circle cx='32' cy='32' r='5' fill='#222'/></svg>"""

DESKTOP = """[Desktop Entry]
Type=Application
Name=Nitro: вентиляторы и мощность
Comment=Тихий режим, полная мощность, кривые вентиляторов
Exec=python3 /home/ruslan/nitro-fan/nitro-gui.py --minimized
Icon=nitro-fans
Categories=System;HardwareSettings;
Terminal=false
"""


def read_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() or "1.0.0"
    except OSError:
        return "1.0.0"


def install_launcher():
    icon_dir = os.path.expanduser("~/.local/share/icons/hicolor/scalable/apps")
    os.makedirs(icon_dir, exist_ok=True)
    with open(os.path.join(icon_dir, "nitro-fans.svg"), "w") as f:
        f.write(SVG)
    apps_dir = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps_dir, exist_ok=True)
    with open(os.path.join(apps_dir, "nitro-fans.desktop"), "w") as f:
        f.write(DESKTOP)


class NitroGui(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="ru.nitro.fans")
        self.win = None
        self.ind = None
        self.stack = None
        self.mode_buttons = {}
        self.lbl_mode = None
        self.team_rows = {}
        self.fan_rows = {}
        self.pb_fans = {}
        self.txt_temp = {}
        self.cpu_spins = []
        self.gpu_spins = []
        self.lbl_curve = None
        self.lbl_epptext = None
        self.last = {}
        self.busy = False

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if HAS_TRAY:
            self.make_tray()

    def do_activate(self):
        self.hold()
        if "--minimized" not in sys.argv:
            self.open_window()
        threading.Thread(target=self.poll_loop, daemon=True).start()

    def poll_loop(self):
        import time
        while True:
            d = read_status()
            GLib.idle_add(self.refresh_ui, d)
            time.sleep(2)

    def make_tray(self):
        self.ind = AppIndicator.Indicator.new(
            "nitro-fans", ICON_NAME, AppIndicator.IndicatorCategory.HARDWARE)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        for mode in ("quiet", "balanced", "performance"):
            item = Gtk.MenuItem(label=MODE_LABELS[mode])
            item.connect("activate", lambda w, m=mode: self.on_mode(m))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        open_item = Gtk.MenuItem(label="Открыть окно")
        open_item.connect("activate", lambda w: self.open_window())
        menu.append(open_item)
        quit_item = Gtk.MenuItem(label="Выход")
        quit_item.connect("activate", lambda w: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.ind.set_menu(menu)

    def open_window(self):
        if self.win is not None:
            self.win.present()
            return
        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_title(APP_TITLE)
        self.win.set_icon_name(ICON_NAME)
        self.win.set_default_size(660, 580)
        self.win.set_border_width(14)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_margin_top(2)
        self.stack.set_margin_bottom(2)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)

        head = Gtk.HeaderBar()
        head.set_show_close_button(True)
        head.set_custom_title(switcher)
        btn_about = Gtk.Button.new_from_icon_name("help-about-symbolic",
                                                  Gtk.IconSize.BUTTON)
        btn_about.connect("clicked", self.on_about)
        head.pack_end(btn_about)
        self.win.set_titlebar(head)

        self.stack.add_titled(self.build_status_tab(), "status", "Статус")
        self.stack.add_titled(self.build_modes_tab(), "modes", "Режимы")
        self.stack.add_titled(self.build_fans_tab(), "fans", "Вентиляторы")

        self.win.add(self.stack)
        self.win.show_all()
        self.win.connect("delete-event", self.on_delete)

    def on_delete(self, win, event):
        if HAS_TRAY:
            win.hide()
            return True
        self.win = None
        self.quit()
        return False

    def build_status_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

        temp_grid = Gtk.Grid(column_spacing=52, halign=Gtk.Align.CENTER)
        self.txt_temp.clear()
        for col, key, caption in ((0, "cpu", "Процессор"), (1, "gpu", "Видеокарта")):
            cap = Gtk.Label(label=caption)
            cap.get_style_context().add_class("title")
            cap.set_halign(Gtk.Align.CENTER)
            big = Gtk.Label(label="—")
            big.get_style_context().add_class("temp")
            big.set_halign(Gtk.Align.CENTER)
            small = Gtk.Label(label="")
            small.get_style_context().add_class("muted")
            small.set_halign(Gtk.Align.CENTER)
            temp_grid.attach(cap, col, 0, 1, 1)
            temp_grid.attach(big, col, 1, 1, 1)
            temp_grid.attach(small, col, 2, 1, 1)
            self.txt_temp[key] = (big, small)
        box.pack_start(temp_grid, False, False, 0)

        self.team_rows = {}
        frame = Gtk.Frame(label="Система")
        frame.get_style_context().add_class("block")
        inner = Gtk.Grid(row_spacing=9, column_spacing=20,
                         margin_start=18, margin_end=18,
                         margin_top=12, margin_bottom=12)
        rows = [
            ("profile", "Профиль мощности"),
            ("epp", "Политика CPU (EPP)"),
            ("fmax", "Потолок частоты"),
            ("overlay", "Тихий оверлей"),
            ("unit", "Автозагрузка"),
        ]
        for r, (k, cap) in enumerate(rows):
            kl = Gtk.Label(label=cap, xalign=0)
            kl.get_style_context().add_class("key")
            vl = Gtk.Label(label="…", xalign=0)
            vl.set_hexpand(True)
            vl.set_ellipsize(1)
            inner.attach(kl, 0, r, 1, 1)
            inner.attach(vl, 1, r, 1, 1)
            self.team_rows[k] = vl
        frame.add(inner)
        box.pack_start(frame, False, False, 0)

        self.fan_rows = {}
        self.pb_fans = {}
        frame = Gtk.Frame(label="Вентиляторы (NBFC)")
        frame.get_style_context().add_class("block")
        inner = Gtk.Grid(row_spacing=10, column_spacing=14,
                         margin_start=18, margin_end=18,
                         margin_top=12, margin_bottom=12)
        kl = Gtk.Label(label="Конфиг NBFC", xalign=0)
        kl.get_style_context().add_class("key")
        vl = Gtk.Label(label="…", xalign=0)
        vl.set_hexpand(True)
        vl.set_ellipsize(1)
        inner.attach(kl, 0, 0, 1, 1)
        inner.attach(vl, 1, 0, 2, 1)
        self.team_rows["cfg"] = vl
        for i, name in ((0, "CPU-вентилятор"), (1, "GPU-вентилятор")):
            kl = Gtk.Label(label=name, xalign=0)
            kl.get_style_context().add_class("key")
            pb = Gtk.ProgressBar()
            pb.set_hexpand(True)
            pb.set_valign(Gtk.Align.CENTER)
            vl = Gtk.Label(label="…", xalign=0)
            inner.attach(kl, 0, i + 1, 1, 1)
            inner.attach(pb, 1, i + 1, 1, 1)
            inner.attach(vl, 2, i + 1, 1, 1)
            self.fan_rows[i] = vl
            self.pb_fans[i] = pb
        frame.add(inner)
        box.pack_start(frame, False, False, 0)

        note = Gtk.Label(
            label="Обновление каждые 2 секунды. Закрытие окна прячет его в трей; "
                  "выход — через меню иконки в трее.",
            xalign=0, wrap=True)
        note.get_style_context().add_class("muted")
        box.pack_start(note, False, False, 0)
        return box

    def build_modes_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

        self.lbl_mode = Gtk.Label(label="Текущий режим: …", xalign=0)
        self.lbl_mode.get_style_context().add_class("title")
        box.pack_start(self.lbl_mode, False, False, 0)

        btn_grid = Gtk.Grid(column_spacing=10, hexpand=True)
        for i, mode in enumerate(("quiet", "balanced", "performance")):
            btn = Gtk.Button(label=MODE_LABELS[mode])
            btn.set_hexpand(True)
            btn.get_style_context().add_class("mode")
            btn.connect("clicked", lambda w, m=mode: self.on_mode(m))
            btn_grid.attach(btn, i, 0, 1, 1)
            self.mode_buttons[mode] = btn
        box.pack_start(btn_grid, False, False, 0)

        frame = Gtk.Frame(label="Что делает каждый режим")
        frame.get_style_context().add_class("block")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_start=18, margin_end=18,
                        margin_top=12, margin_bottom=12)
        descs = [
            ("#3a9ced", "Тихий",
             "профиль «Баланс», политика энергии balance_power, потолок частоты "
             "2600 МГц, тихая кривая вентиляторов. Автоматически применяется при "
             "каждой загрузке."),
            ("#a09a93", "Родной баланс",
             "профиль «Баланс» без ограничений CPU: потолка нет, политика "
             "balance_performance. Нагрев и шум — как в стоке."),
            ("#e04545", "Полная мощность",
             "профиль «Полная мощность» без ограничений CPU. Кривая вентиляторов "
             "NBFC сохраняется, но на нагрузке они крутят сильнее."),
        ]
        for color, head, text in descs:
            lab = Gtk.Label(xalign=0, wrap=True, use_markup=True)
            lab.set_markup(
                "<span foreground='%s'>●</span>  <b>%s</b> — %s"
                % (color, head, text))
            inner.pack_start(lab, False, False, 0)
        frame.add(inner)
        box.pack_start(frame, False, False, 0)

        self.lbl_epptext = Gtk.Label(
            label="Кнопки «Тихий» и «Полная мощность» запрашивают пароль (pkexec). "
                  "Фиксация скорости и кривые вентиляторов работают без пароля.",
            xalign=0, wrap=True)
        self.lbl_epptext.get_style_context().add_class("muted")
        box.pack_start(self.lbl_epptext, False, False, 0)

        self.chk_autostart = Gtk.CheckButton(
            label="Запускать приложение при входе в систему (свёрнутым в трей)")
        self.chk_autostart.set_active(os.path.exists(AUTOSTART_FILE))
        self.chk_autostart.connect("toggled", self.on_autostart_toggle)
        box.pack_start(self.chk_autostart, False, False, 0)
        return box

    def on_autostart_toggle(self, chk):
        try:
            if chk.get_active():
                os.makedirs(AUTOSTART_DIR, exist_ok=True)
                with open(AUTOSTART_FILE, "w") as f:
                    f.write(DESKTOP)
            else:
                os.remove(AUTOSTART_FILE)
        except OSError as e:
            if self.lbl_mode:
                self.lbl_mode.set_text("Автозапуск приложения: ошибка (%s)" % e)
            chk.set_active(not chk.get_active())
            return
        if self.lbl_mode:
            self.lbl_mode.set_text("Автозапуск приложения: %s" %
                                   ("включен" if chk.get_active()
                                    else "отключен"))

    def on_about(self, w):
        dlg = Gtk.AboutDialog()
        if self.win:
            dlg.set_transient_for(self.win)
            dlg.set_modal(True)
        dlg.set_program_name("nitro-fan")
        dlg.set_version(read_version())
        dlg.set_comments("Управление вентиляторами и мощностью "
                         "Acer Nitro AN515-55 под Linux.")
        dlg.set_license("MIT")
        dlg.set_website("https://github.com/rsmagulov/nitro-fan")
        dlg.set_website_label("github.com/rsmagulov/nitro-fan")
        dlg.set_logo_icon_name(ICON_NAME)
        dlg.run()
        dlg.destroy()

    def build_fans_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

        row = Gtk.Box(spacing=8)
        self.spin_fixed = Gtk.SpinButton.new_with_range(5, 100, 1)
        self.spin_fixed.set_value(40)
        btn_fix = Gtk.Button(label="Зафиксировать")
        btn_fix.connect("clicked", self.on_fan_fix)
        btn_auto = Gtk.Button(label="Вернуть NBFC-авто")
        btn_auto.connect("clicked", self.on_fan_auto)
        row.pack_start(Gtk.Label(label="Фиксированная скорость, %:",
                                 xalign=1), True, True, 0)
        row.pack_start(self.spin_fixed, False, False, 0)
        row.pack_start(btn_fix, False, False, 0)
        row.pack_start(btn_auto, False, False, 0)
        box.pack_start(row, False, False, 0)

        frame = Gtk.Frame(label="Кривая по температуре")
        frame.get_style_context().add_class("block")
        grid = Gtk.Grid(row_spacing=4, column_spacing=24, halign=Gtk.Align.CENTER,
                        margin_start=18, margin_end=18,
                        margin_top=12, margin_bottom=12)
        for c, cap in enumerate(("порог, °C", "CPU, %", "GPU, %")):
            h = Gtk.Label(label=cap)
            h.get_style_context().add_class("title")
            grid.attach(h, c, 0, 1, 1)
        self.cpu_vals, self.gpu_vals = load_curve()
        for i, t in enumerate(THRESHOLDS):
            lab = Gtk.Label(label=str(t))
            lab.set_halign(Gtk.Align.END)
            lab.get_style_context().add_class("key")
            grid.attach(lab, 0, i + 1, 1, 1)
            sp1 = Gtk.SpinButton.new_with_range(0, 100, 1)
            sp1.set_value(self.cpu_vals[i])
            sp1.connect("value-changed", self.on_curve_off)
            grid.attach(sp1, 1, i + 1, 1, 1)
            sp2 = Gtk.SpinButton.new_with_range(0, 100, 1)
            sp2.set_value(self.gpu_vals[i])
            sp2.connect("value-changed", self.on_curve_off)
            grid.attach(sp2, 2, i + 1, 1, 1)
            self.cpu_spins.append(sp1)
            self.gpu_spins.append(sp2)
        frame.add(grid)
        box.pack_start(frame, False, False, 0)

        presets = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        b1 = Gtk.Button(label="Пресет «Тихий»")
        b1.connect("clicked", lambda w: self.apply_preset(BASE_CURVE))
        b2 = Gtk.Button(label="Пресет «Динамичный»")
        b2.connect("clicked", lambda w: self.apply_preset(SPORT_CURVE))
        presets.pack_start(b1, False, False, 0)
        presets.pack_start(b2, False, False, 0)
        box.pack_start(presets, False, False, 0)

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        b3 = Gtk.Button(label="Применить кривую")
        b3.connect("clicked", self.on_apply_curve)
        b4 = Gtk.Button(label="Сброс к системному Quiet")
        b4.connect("clicked", self.on_reset_curve)
        actions.pack_start(b3, False, False, 0)
        actions.pack_start(b4, False, False, 0)
        box.pack_start(actions, False, False, 0)

        self.lbl_curve = Gtk.Label(label="", xalign=0, wrap=True)
        self.lbl_curve.get_style_context().add_class("muted")
        box.pack_start(self.lbl_curve, False, False, 0)
        return box

    def apply_preset(self, preset):
        self.cpu_vals = list(preset["cpu"])
        self.gpu_vals = list(preset["gpu"])
        for s, v in zip(self.cpu_spins, self.cpu_vals):
            s.set_value(v)
        for s, v in zip(self.gpu_spins, self.gpu_vals):
            s.set_value(v)

    def on_curve_off(self, sp):
        self.cpu_vals = [int(s.get_value()) for s in self.cpu_spins]
        self.gpu_vals = [int(s.get_value()) for s in self.gpu_spins]

    def on_mode(self, mode):
        if self.busy:
            return
        self.busy = True
        if self.win:
            self.win.set_sensitive(False)
        if self.lbl_mode:
            self.lbl_mode.set_text("Применение: %s …" % MODE_LABELS[mode])

        def worker():
            r1, r2 = mode_action(mode)
            GLib.idle_add(self.after_mode, mode, r1, r2)
        threading.Thread(target=worker, daemon=True).start()

    def after_mode(self, mode, r1, r2):
        if r1[0] == 0 and r2[0] == 0:
            text = "Готово: %s" % MODE_LABELS[mode]
        else:
            text = ("Не удалось (%s): профиль rc=%d, pkexec rc=%d: %s"
                    % (MODE_LABELS[mode], r1[0], r2[0], (r2[2] or r1[2])[:300]))
        if self.lbl_mode:
            self.lbl_mode.set_text(text)
        if self.win:
            self.win.set_sensitive(True)
        self.busy = False
        self.highlight_mode()
        return False

    def highlight_mode(self):
        cur = detect_mode(self.last) if self.last else None
        for mode, btn in self.mode_buttons.items():
            ctx = btn.get_style_context()
            if mode == cur:
                ctx.add_class("active")
            else:
                ctx.remove_class("active")

    def on_fan_fix(self, w):
        pct = int(self.spin_fixed.get_value())
        rc, out, err = run(["nbfc", "set", "-s", str(pct)])
        self.flash("Зафиксировать %.0f%%: rc=%d %s" %
                   (self.spin_fixed.get_value(), rc, (err or out).strip()[:180]))

    def on_fan_auto(self, w):
        rc, out, err = run(["nbfc", "set", "--auto"])
        self.flash("Возврат в NBFC-авто: rc=%d %s" %
                   (rc, (err or out).strip()[:180]))

    def on_apply_curve(self, w):
        self.cpu_vals = [int(s.get_value()) for s in self.cpu_spins]
        self.gpu_vals = [int(s.get_value()) for s in self.gpu_spins]
        if not self.non_decreasing(self.cpu_vals) or \
           not self.non_decreasing(self.gpu_vals):
            self.flash("Скорости по порогам должны не убывать (0 → 100).")
            return
        save_curve(self.cpu_vals, self.gpu_vals)
        if self.win:
            self.win.set_sensitive(False)
        self.flash("Применяется кривая…")

        def worker():
            rc, out, err = apply_custom_curve(self.cpu_vals, self.gpu_vals)
            GLib.idle_add(self.after_apply_curve, rc, err)
        threading.Thread(target=worker, daemon=True).start()

    def after_apply_curve(self, rc, err):
        if self.win:
            self.win.set_sensitive(True)
        self.flash("Кривая применена: rc=%d %s" % (rc, err.strip()[:180]))
        return False

    def on_reset_curve(self, w):
        if self.win:
            self.win.set_sensitive(False)
        self.flash("Сбрасывается…")

        def worker():
            rc, out, err = reset_curve()
            GLib.idle_add(self.after_apply_curve, rc, err)
        threading.Thread(target=worker, daemon=True).start()

    def non_decreasing(self, vals):
        return all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))

    def flash(self, text):
        if self.lbl_curve:
            self.lbl_curve.set_text(text)
            GLib.timeout_add_seconds(7, self._flash_clear, text)

    def _flash_clear(self, text):
        if self.lbl_curve and self.lbl_curve.get_text() == text:
            self.lbl_curve.set_text("")
        return False

    def refresh_ui(self, d):
        self.last = d
        for key, fan_name in (("cpu", "CPU fan"), ("gpu", "GPU fan")):
            big, small = self.txt_temp.get(key, (None, None))
            if big is None:
                continue
            temp = d.get(key)
            fan = next((f for f in d.get("fans", [])
                        if f.get("name") == fan_name), None)
            ctx = big.get_style_context()
            ctx.remove_class("warm")
            ctx.remove_class("hot")
            if temp is not None and fan:
                rpm = int(round(fan["cur"] / 100.0 * fan["steps"]))
                small.set_text("~%d об/мин, цель %.0f%%" % (rpm, fan["tgt"]))
                big.set_text("%d°" % temp)
                if temp >= 85:
                    ctx.add_class("hot")
                elif temp >= 75:
                    ctx.add_class("warm")
            elif temp is not None:
                big.set_text("%d°" % temp)
                small.set_text("вентилятор: н/д")
            else:
                big.set_text("—")
                small.set_text("нет данных")

        if self.team_rows:
            prof = d.get("profile")
            self.team_rows["profile"].set_text(
                PROF_RU.get(prof, prof or "н/д"))
            epp = d.get("epp") or ""
            self.team_rows["epp"].set_text(
                "%s  (%s)" % (epp or "н/д", EPP_RU.get(epp, "неизвестный")))
            self.team_rows["fmax"].set_text("%s МГц" % (d.get("max") or "?"))
            self.team_rows["overlay"].set_text(
                "включен" if d.get("quiet_active") else "выключен")
            self.team_rows["unit"].set_text(
                "включена" if d.get("unit") else "отключена")
            self.team_rows["cfg"].set_text(d.get("nbfc_cfg") or "н/д")

        fans = d.get("fans", [])
        for i in (0, 1):
            pl = self.pb_fans.get(i)
            vl = self.fan_rows.get(i)
            if pl is None:
                continue
            f = fans[i] if len(fans) > i else None
            ctx = pl.get_style_context()
            for cls in ("calm", "warn", "hot"):
                ctx.remove_class(cls)
            if f:
                pl.set_fraction(max(0.0, min(1.0, f["cur"] / 100.0)))
                ctx.add_class(fan_zone(f))
                rpm = int(round(f["cur"] / 100.0 * (f["steps"] or 1)))
                vl.set_text("~%d об/мин · %.1f%% · цель %.1f%% · %.1f°"
                            % (rpm, f["cur"], f["tgt"], f["temp"]))
            else:
                pl.set_fraction(0)
                vl.set_text("н/д")

        if self.lbl_mode:
            mode = detect_mode(d)
            if not self.busy:
                self.lbl_mode.set_text("Текущий режим: %s" %
                                       MODE_LABELS[mode])
        self.highlight_mode()
        return False


def main():
    install_launcher()
    app = NitroGui()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
