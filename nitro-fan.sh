#!/usr/bin/env bash
set -euo pipefail

NAME="nitro-fan"
CACHE_DIR="${XDG_CACHE_HOME:-}"
[ -z "$CACHE_DIR" ] && { [ -n "${HOME:-}" ] && CACHE_DIR="$HOME/.cache" || CACHE_DIR="/tmp/nitro-fan-cache"; }
CACHE_DIR="$CACHE_DIR/nitro-fan"
STATE_FILE="$CACHE_DIR/cpu-quiet.state"
NBFC_BIN="$(command -v nbfc 2>/dev/null || true)"
NVIDIA_SMI_BIN="$(command -v nvidia-smi 2>/dev/null || true)"
POLL_INTERVAL=5
SPEED_STEP_MIN=5

CPU_LADDER=(55:0 60:20 65:30 70:40 75:55 80:75 85:90 90:100)
GPU_LADDER=(50:0 55:20 60:30 65:40 70:55 75:75 80:90 85:100)
CPU_EMERGENCY=87
GPU_EMERGENCY=83

EC_IO="/sys/kernel/debug/ec/ec0/io"
EC_REG_CPU_FAN_SPEED=55
EC_REG_GPU_FAN_SPEED=58
EC_REG_CPU_FAN_MANUAL=34
EC_REG_GPU_FAN_MANUAL=33
EC_REG_GLOBAL_MANUAL=151
EC_REG_CPU_FAN_RPM_LO=19
EC_VAL_MANUAL_CPU=12
EC_VAL_MANUAL_GPU=48
EC_VAL_MANUAL_GLOBAL=1
EC_VAL_AUTO_CPU=4
EC_VAL_AUTO_GPU=16
EC_VAL_AUTO_GLOBAL=0
EC_TEST_SECONDS=90
EC_TEST_INTERVAL=0.5
EC_STOP_ABORT_CPU=75
EC_STOP_ABORT_GPU=65

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

find_cpu_temp_file() {
    local h name
    for h in /sys/class/hwmon/hwmon*; do
        name="$(cat "$h/name" 2>/dev/null || true)"
        if [ "$name" = "coretemp" ] && [ -r "$h/temp1_input" ]; then
            printf '%s' "$h/temp1_input"
            return 0
        fi
    done
    for h in /sys/class/thermal/thermal_zone*; do
        case "$(cat "$h/type" 2>/dev/null)" in
            x86_pkg_temp) [ -r "$h/temp" ] && { printf '%s' "$h/temp"; return 0; } ;;
            B0D4) [ -r "$h/temp" ] && { printf '%s' "$h/temp"; return 0; } ;;
        esac
    done
    return 1
}

read_cpu_temp() {
    printf '%s\n' "$(( $(cat "$CPU_TEMP_FILE" 2>/dev/null || echo 0) / 1000 ))"
}

read_gpu_temp() {
    local t
    if [ -n "$NVIDIA_SMI_BIN" ]; then
        t="$(timeout 3 "$NVIDIA_SMI_BIN" --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null | head -n1 || true)"
        [ -n "${t:-}" ] && [ "$t" -lt 150 ] 2>/dev/null && { printf '%s' "$t"; return 0; }
    fi
    for h in /sys/class/thermal/thermal_zone*; do
        case "$(cat "$h/type" 2>/dev/null)" in
            PN0D|NVKM|GPU|ambtemp) [ -r "$h/temp" ] && { printf '%s\n' "$(( $(cat "$h/temp") / 1000 ))"; return 0; } ;;
        esac
    done
    printf '0'
}

ladder_speed() {
    local dist="$1" step key_t key_s speed=0
    local -a ladder
    [ "$dist" = "cpu" ] && ladder=("${CPU_LADDER[@]}") || ladder=("${GPU_LADDER[@]}")
    for step in "${ladder[@]}"; do
        key_t="${step%%:*}"; key_s="${step##*:}"
        [ "$2" -ge "$key_t" ] && speed="$key_s"
    done
    printf '%s' "$speed"
}

need_root() {
    [ "$(id -u)" -eq 0 ] || die "команда '$1' требует root: запускайте 'sudo $0 $1'"
}

cmd_status() {
    local cpu gpu nvme h nv_name
    cpu="$(read_cpu_temp)"
    gpu="$(read_gpu_temp)"
    nvme=""
    for h in /sys/class/hwmon/hwmon*; do
        nv_name="$(cat "$h/name" 2>/dev/null || true)"
        if [ "$nv_name" = "nvme" ] && [ -r "$h/temp1_input" ]; then
            nvme="$(awk '{printf "%.1f°C", $1/1000}' "$h/temp1_input")"
            break
        fi
    done
    nvme="${nvme:-—}"
    log "CPU: ${cpu}°C | GPU: $([ "$gpu" -gt 0 ] 2>/dev/null && printf '%s°C' "$gpu" || echo 'нет данных') | NVMe: ${nvme}"
    if [ -n "$NBFC_BIN" ]; then
        "$NBFC_BIN" status -a 2>/dev/null || log "NBFC-сервис не запущен"
    else
        log "NBFC-Linux не установлен (fancontrol невозможен напрямую: pwm/fan интерфейсы в hwmon отсутствуют)"
    fi
    local p f
    p=$(powerprofilesctl get 2>/dev/null || echo '?')
    f=$(cat /sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference 2>/dev/null)
    log "Power profile: ${p}, EPP: ${f}, CPU max: $(( $(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq) / 1000 )) МГц"
}

print_unit_cpuquiet() {
    cat <<EOF
[Unit]
Description=Apply Nitro CPU quiet limits (EPP + freq cap) at boot
Wants=power-profiles-daemon.service thermald.service
After=power-profiles-daemon.service thermald.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nitro-fan cpu-quiet-apply 2600000
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target

EOF
}

print_unit_timer() {
    cat <<EOF
nitro-cpu-quiet-assert.service:
[Unit]
Description=Re-assert Nitro CPU quiet limits (only when nitro-cpu-quiet enabled)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nitro-fan cpu-quiet-assert

nitro-cpu-quiet-assert.timer:
[Unit]
Description=Periodic re-assert of Nitro CPU quiet limits

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
Unit=nitro-cpu-quiet-assert.service

[Install]
WantedBy=timers.target

EOF
}

cmd_cpu_quiet() {
    local mode="${1:-}" max_mhz="${2:-2600000}"
    case "$mode" in
        on)
            need_root "cpu-quiet on"
            powerprofilesctl set balanced 2>/dev/null || true
            local pol save
            mkdir -p "$CACHE_DIR"
            : > "$STATE_FILE"
            for pol in /sys/devices/system/cpu/cpufreq/policy*; do
                save="$(cat "$pol/energy_performance_preference")"
                printf '%s %s %s\n' "$pol" "$save" "$(cat "$pol/scaling_max_freq")" >> "$STATE_FILE"
            done
            for pol in /sys/devices/system/cpu/cpufreq/policy*; do
                echo "balance_power" > "$pol/energy_performance_preference"
                echo "$max_mhz"     > "$pol/scaling_max_freq"
            done
            echo "on" > "$CACHE_DIR/cpu-quiet.active"
            systemctl enable nitro-cpu-quiet.service 2>/dev/null || true
            log "CPU-quiet включён: EPP=balance_power, потолок частоты ${max_mhz} (автозагрузка включена)"
            ;;
        apply)
            need_root "cpu-quiet-apply"
            for pol in /sys/devices/system/cpu/cpufreq/policy*; do
                echo "balance_power" > "$pol/energy_performance_preference" 2>/dev/null || true
                echo "$max_mhz"      > "$pol/scaling_max_freq" 2>/dev/null || true
            done
            echo "on" > "$CACHE_DIR/cpu-quiet.active"
            log "CPU-quiet применён: EPP=balance_power, потолок ${max_mhz}"
            ;;
        assert)
            need_root "cpu-quiet-assert"
            systemctl is-enabled nitro-cpu-quiet.service >/dev/null 2>&1 || exit 0
            for pol in /sys/devices/system/cpu/cpufreq/policy*; do
                echo "balance_power" > "$pol/energy_performance_preference" 2>/dev/null || true
                echo "$max_mhz"      > "$pol/scaling_max_freq" 2>/dev/null || true
            done
            log "CPU-quiet re-assert: EPP=balance_power, потолок ${max_mhz}"
            ;;
        off)
            need_root "cpu-quiet off"
            [ -r "$STATE_FILE" ] || die "нет сохранённого состояния (cpu-quiet не включался)"
            local line pol epp max
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                read -r pol epp max <<< "$line"
                printf '%s\n' "$epp" > "$pol/energy_performance_preference" 2>/dev/null || true
                printf '%s\n' "$max" > "$pol/scaling_max_freq" 2>/dev/null || true
            done < "$STATE_FILE"
            rm -f "$CACHE_DIR/cpu-quiet.active"
            systemctl disable nitro-cpu-quiet.service 2>/dev/null || true
            log "CPU-quiet выключен, значения восстановлены, автозагрузка отключена"
            ;;
        *)
            die "использование: $0 cpu-quiet [on|off]"
            ;;
    esac
}

cmd_watch() {
    local interval="${1:-3}"; [[ "$interval" =~ ^[0-9]+$ ]] || die "использование: $0 watch <секунды>"
    while :; do
        cmd_status
        echo "---"
        sleep "$interval"
    done
}

fan_exist() {
    [ -n "$NBFC_BIN" ] || die "NBFC-Linux не установлен. Установите: см. README-RU.md ( раздел 'Установка NBFC' )"
    "$NBFC_BIN" status -s >/dev/null 2>&1 || die "NBFC-сервис не запущен: sudo nbfc start / sudo systemctl start nbfc_service"
}

cmd_fan_speed() {
    local pct="${1:-}"
    [[ "$pct" =~ ^[0-9]+$ ]] && [ "$pct" -le 100 ] || die "использование: $0 fan-speed <0..100>"
    fan_exist
    "$NBFC_BIN" set -s "$pct" && log "Задана скорость вентиляторов: ${pct}%"
}

cmd_fan_auto() {
    fan_exist
    "$NBFC_BIN" set --auto && log "Вентиляторы возвращены под управление NBFC-профиля"
}

fan_needs_update() {
    local cur="$1" new="$2"
    [ "$cur" != "$new" ] || return 1
    local diff=$(( new - cur )); diff=${diff#-}
    [ "$diff" -ge "$SPEED_STEP_MIN" ] || return 1
    return 0
}

cmd_fan_curve() {
    fan_exist
    local trap_installed
    trap_installed=0
    restore_auto() {
        if [ "$trap_installed" -eq 1 ]; then
            log "Выход: возвращаю вентиляторы в auto-режим NBFC"
            "$NBFC_BIN" set --auto 2>/dev/null || true
        fi
    }
    trap restore_auto EXIT INT TERM
    trap_installed=1

    local cpu gpu s_cpu s_gpu target applied=999 prev_applied=999
    log "Запущена тихая кривая: CPU-лестница (${CPU_LADDER[*]}), GPU (${GPU_LADDER[*]})"
    log "Аварийные пороги: CPU>${CPU_EMERGENCY}°C или GPU>${GPU_EMERGENCY}°C -> 100%"
    log "Ctrl+C — стоп и возврат в auto"

    while :; do
        cpu="$(read_cpu_temp)"
        gpu="$(read_gpu_temp)"
        s_cpu="$(ladder_speed cpu "$cpu")"
        s_gpu="$(ladder_speed gpu "$gpu")"
        target="$s_cpu"; [ "$s_gpu" -gt "$target" ] && target="$s_gpu"

        if [ "$cpu" -ge "$CPU_EMERGENCY" ] || [ "$gpu" -ge "$GPU_EMERGENCY" ]; then
            log "АВАРИЯ: CPU ${cpu}°C GPU ${gpu}°C -> 100%"
            "$NBFC_BIN" set -s 100 || true
            applied=100
            sleep 2
            continue
        fi

        if fan_needs_update "$applied" "$target"; then
            if "$NBFC_BIN" set -s "$target"; then
                log "CPU ${cpu}°C GPU ${gpu}°C -> вентиляторы ${target}%"
                prev_applied="$applied"
                applied="$target"
                [ "$target" -eq 0 ] && [ "$prev_applied" -gt 0 ] && sleep 1
            else
                applied="$prev_applied"
            fi
        else
            log "CPU ${cpu}°C GPU ${gpu}°C -> вентиляторы ${target}% (без изменений)"
        fi
        sleep "$POLL_INTERVAL"
    done
}

print_nbfc_install_steps() {
    local repo_config="/home/ruslan/nitro-fan/nbfc-quiet-curve.json"
    [ -f "$repo_config" ] || repo_config="$PWD/nbfc-quiet-curve.json"
    cat <<EOF
Установка NBFC-Linux на Linux Mint 22 (актуально для этой машины, проверено):

    cd ~/Downloads
    curl -LO https://github.com/nbfc-linux/nbfc-linux/releases/download/0.5.4/linux-mint-22-nbfc-linux_0.5.4_amd64.deb
    sudo apt install ./linux-mint-22-nbfc-linux_0.5.4_amd64.deb
    sudo modprobe ec_sys write_support=1

Постоянная загрузка ec_sys с write-режимом (переживает перезагрузку):

    echo 'options ec_sys write_support=1' | sudo tee /etc/modprobe.d/ec_sys.conf
    echo 'ec_sys' | sudo tee /etc/modules-load.d/ec_sys.conf

Проверка в режиме ЧТЕНИЯ (не пишет в EC, безопасно смотреть обороты):

    sudo systemctl disable nbfc_service 2>/dev/null
    sudo nbfc start -r
    nbfc status -a      # RPM должны быть правдоподобны (CPU ~2000, GPU ~2300)

Установка тихого конфига (репозитория nitro-fan):

    sudo mkdir -p /var/lib/nbfc/configs
    sudo cp '$repo_config' '/var/lib/nbfc/configs/Acer Nitro AN515-55 Quiet.json'
    sudo nbfc config --set 'Acer Nitro AN515-55 Quiet'
    sudo nbfc sensors set -f 1 -s coretemp -a Max

Включение write-режима, проверка writes и автозапуск:

    sudo nbfc restart
    nbfc set -s 40; sleep 5; nbfc set --auto
    sudo systemctl enable nbfc_service

Опционально (когда NBFC не активен): демон тихой кривой '$0 fan-curve'.
EOF
}

ec_byte_read() {
    local v
    v="$(dd if="$EC_IO" bs=1 skip="$1" count=1 2>/dev/null | od -An -tu1 2>/dev/null | tr -d ' \n')"
    [ -n "$v" ] || return 1
    printf '%s' "$v"
}

ec_val() {
    printf '%s' "$(ec_byte_read "$1" 2>/dev/null || echo '?')"
}

ec_word_rpm() {
    local lo hi a b
    lo="$(ec_byte_read "$1")"
    hi="$(ec_byte_read "$(($1 + 1))")"
    a=$(( hi * 256 + lo )); b=$(( lo * 256 + hi ))
    for v in "$a" "$b"; do
        [ "$v" -gt 500 ] && [ "$v" -lt 20000 ] && { printf '%s' "$v"; return 0; }
    done
    printf '%s' "$b"
}

ec_write_byte() {
    printf "\\x$(printf %02x "$2")" | dd of="$EC_IO" bs=1 seek="$1" conv=notrunc 2>/dev/null || return 1
}

ec_restore_loop() {
    local i
    for i in 1 2 3; do
        ec_write_byte "$EC_REG_CPU_FAN_MANUAL" "$EC_VAL_AUTO_CPU" || true
        ec_write_byte "$EC_REG_GPU_FAN_MANUAL" "$EC_VAL_AUTO_GPU" || true
        ec_write_byte "$EC_REG_GLOBAL_MANUAL" "$EC_VAL_AUTO_GLOBAL" || true
        sleep 0.4
    done
}

cmd_ec_pre() {
    need_root "ec-prep"
    lsmod | grep -q '^ec_sys' || modprobe ec_sys || die "ec_sys не загрузился"
    [ -e "$EC_IO" ] || sleep 1
    [ -e "$EC_IO" ] || die "ec0/io не появился в debugfs"
    chmod a+rx /sys/kernel/debug 2>/dev/null || true
    chmod a+rx /sys/kernel/debug/ec /sys/kernel/debug/ec/ec0 2>/dev/null || true
    chmod a+r "$EC_IO"
    echo "ec_sys загружен (без write_support), $EC_IO доступен на чтение"
}

cmd_ec_read() {
    local c g cw gw
    c="$(ec_val "$EC_REG_CPU_FAN_SPEED")"
    g="$(ec_val "$EC_REG_GPU_FAN_SPEED")"
    cw="$(ec_val "$EC_REG_CPU_FAN_MANUAL")"
    gw="$(ec_val "$EC_REG_GPU_FAN_MANUAL")"
    local rpm_c rpm_g
    rpm_c="$(ec_word_rpm "$EC_REG_CPU_FAN_RPM_LO" 2>/dev/null || echo '?')"
    rpm_g="$(ec_word_rpm 21 2>/dev/null || echo '?')"
    echo "EC 0x37 CPU speed = ${c}, 0x3A GPU speed = ${g}"
    echo "EC 0x22 CPU manual = ${cw}, 0x21 GPU manual = ${gw}, 0x97 global = $(ec_byte_read "$EC_REG_GLOBAL_MANUAL")"
    echo "RPM: CPU ≈ ${rpm_c}, GPU ≈ ${rpm_g}"
    local sanity=1
    for v in $c $g $cw $gw $rpm_c $rpm_g; do
        [[ "$v" =~ ^[0-9]+$ ]] || sanity=0
    done
    if [ "$sanity" -eq 1 ] && [ "$c" -le 100 ] && [ "$g" -le 100 ] && [ "$cw" -le 48 ] && [ "$gw" -le 48 ] \
       && [[ "$rpm_c" =~ ^[0-9]+$ ]] && [[ "$rpm_g" =~ ^[0-9]+$ ]] && [ "$rpm_c" -lt 20000 ] && [ "$rpm_g" -lt 20000 ]; then
        sanity=1
    else
        sanity=0
    fi
    [ "$sanity" -eq 1 ] && echo "SANITY: ок — раскладка AN515-44/-54 совпадает" || echo "SANITY: НЕСОВПАДЕНИЕ — прямые записи в EC запрещены"
}

cmd_ec_test() {
    local seconds="${1:-$EC_TEST_SECONDS}"
    [[ "$seconds" =~ ^[0-9]+$ ]] && [ "$seconds" -le 1800 ] || die "использование: ec-test [секунды, 0-1800]"
    need_root "ec-test"
    lsmod | grep -q '^ec_sys' || die "ec_sys не загружен"
    rmmod ec_sys 2>/dev/null || true
    modprobe ec_sys write_support=1 || die "не удалось включить write_support"
    [ -e "$EC_IO" ] || die "$EC_IO не появился"
    chmod a+rx /sys/kernel/debug 2>/dev/null || true
    chmod a+rx /sys/kernel/debug/ec /sys/kernel/debug/ec/ec0 2>/dev/null || true
    chmod a+r "$EC_IO"
    if ! ec_byte_read "$EC_REG_CPU_FAN_SPEED" >/dev/null 2>&1; then die "EC не отвечает"; fi
    cmd_ec_read
    local cpu gpu
    cpu="$(read_cpu_temp)"; gpu="$(read_gpu_temp)"
    if [ "$cpu" -ge "$EC_STOP_ABORT_CPU" ] || ([ "$gpu" -gt 0 ] 2>/dev/null && [ "$gpu" -ge "$EC_STOP_ABORT_GPU" ]); then
        die "температуры слишком высоки для теста (CPU ${cpu}°C GPU ${gpu}°C), стоп-тест отменён"
    fi

    local end_t deadline start elapsed rpm_c rpm_g
    start="$(date +%s%N)"
    deadline=$(( start + seconds * 1000000000 ))
    trap 'echo; echo "Восстанавливаю авто-режим EC..."; ec_restore_loop; exit 130' INT TERM
    echo "СТОП-ТЕСТ: ручные биты + 0% каждые ${EC_TEST_INTERVAL}с, $seconds сек; аварию CPU ${EC_STOP_ABORT_CPU}/GPU ${EC_STOP_ABORT_GPU}"
    printf '%-9s %-10s %-10s %-28s %s\n' ВРЕМЯ CPU RPM GPU READBACK
    while :; do
        now="$(date +%s%N)"
        elapsed=$(( (now - start) / 1000000000 ))
        [ "$now" -ge "$deadline" ] && break
        cpu="$(read_cpu_temp)"
        gpu="$(read_gpu_temp)"
        if [ "$cpu" -ge "$EC_STOP_ABORT_CPU" ] || ([ "$gpu" -gt 0 ] && [ "$gpu" -ge "$EC_STOP_ABORT_GPU" ]); then
            echo "Авария: CPU ${cpu}°C GPU ${gpu}°C — немедленное восстановление"
            break
        fi
        if [ "$(( elapsed % 7 ))" = "0" ]; then
            ec_write_byte "$EC_REG_GLOBAL_MANUAL" "$EC_VAL_MANUAL_GLOBAL"
            ec_write_byte "$EC_REG_CPU_FAN_MANUAL" "$EC_VAL_MANUAL_CPU"
            ec_write_byte "$EC_REG_GPU_FAN_MANUAL" "$EC_VAL_MANUAL_GPU"
        fi
        ec_write_byte "$EC_REG_CPU_FAN_SPEED" 0 || true
        ec_write_byte "$EC_REG_GPU_FAN_SPEED" 0 || true
        rpm_c="$(ec_word_rpm "$EC_REG_CPU_FAN_RPM_LO" 2>/dev/null || echo '?')"
        rpm_g="$(ec_word_rpm 21 2>/dev/null || echo '?')"
        printf '%-9s %-7sC %-10s %-10s %s/%s/%s=%s,%s,%s\n' \
            "$(date +%H:%M:%S)" "$cpu" "$rpm_c" "$rpm_g" \
            '0x22' '0x21' '0x97' "$(ec_val "$EC_REG_CPU_FAN_MANUAL")" "$(ec_val "$EC_REG_GPU_FAN_MANUAL")" "$(ec_val "$EC_REG_GLOBAL_MANUAL")"
        sleep "$EC_TEST_INTERVAL"
    done
    echo "Восстановление авто-режима EC..."
    ec_restore_loop
    echo "Тест завершён, EC вернул управление"
}

cmd_ec_max() {
    need_root "ec-max"
    [ -e "$EC_IO" ] || die "EC недоступен: ec-prep"
    local i rpm_c rpm_g
    echo "EC-MAX: 100% на 5 сек (диагностика реакции регистров)"
    ec_write_byte "$EC_REG_GLOBAL_MANUAL" "$EC_VAL_MANUAL_GLOBAL" || true
    ec_write_byte "$EC_REG_CPU_FAN_MANUAL" "$EC_VAL_MANUAL_CPU" || true
    ec_write_byte "$EC_REG_GPU_FAN_MANUAL" "$EC_VAL_MANUAL_GPU" || true
    for i in 1 2 3 4 5 6 7 8 9 10; do
        ec_write_byte "$EC_REG_CPU_FAN_SPEED" 100 || true
        ec_write_byte "$EC_REG_GPU_FAN_SPEED" 100 || true
        rpm_c="$(ec_word_rpm 19 2>/dev/null || echo '?')"
        rpm_g="$(ec_word_rpm 21 2>/dev/null || echo '?')"
        printf '%s RPM: CPU %s GPU %s (speed readback %s/%s)\n' "$(date +%H:%M:%S)" "$rpm_c" "$rpm_g" "$(ec_val 55)" "$(ec_val 58)"
        sleep 0.5
    done
    echo "Восстановление..."
    ec_restore_loop
}

cmd_ec_sweep() {
    need_root "ec-sweep"
    [ -e "$EC_IO" ] || die "EC недоступен: ec-prep"
    local d t rpm_c rpm_g
    echo "EC-SWEEP: лестница duty 40→20→10→5→1→0, по 2 сек на ступень"
    ec_write_byte "$EC_REG_GLOBAL_MANUAL" "$EC_VAL_MANUAL_GLOBAL" || true
    ec_write_byte "$EC_REG_CPU_FAN_MANUAL" "$EC_VAL_MANUAL_CPU" || true
    ec_write_byte "$EC_REG_GPU_FAN_MANUAL" "$EC_VAL_MANUAL_GPU" || true
    for d in 40 20 10 5 1 0; do
        for t in 1 2 3 4; do
            ec_write_byte "$EC_REG_CPU_FAN_SPEED" "$d" || true
            ec_write_byte "$EC_REG_GPU_FAN_SPEED" "$d" || true
            rpm_c="$(ec_word_rpm 19 2>/dev/null || echo '?')"
            rpm_g="$(ec_word_rpm 21 2>/dev/null || echo '?')"
            printf 'duty %3s | RPM: CPU %s GPU %s | readback %s/%s | %s\n' "$d" "$rpm_c" "$rpm_g" "$(ec_val 55)" "$(ec_val 58)" "$(date +%H:%M:%S)"
            sleep 0.5
        done
    done
    echo "Восстановление..."
    ec_restore_loop
}

print_unit() {
    cat <<EOF
[Unit]
Description=Nitro quiet fan curve

[Service]
ExecStart=$SCRIPT_PATH fan-curve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target

Установка:
    sudo install -m 755 '$SCRIPT_PATH' /usr/local/bin/nitro-fan
    sudo tee /etc/systemd/system/nitro-fan-curve.service < <($0 unit)
    sudo systemctl daemon-reload && sudo systemctl enable --now nitro-fan-curve
EOF
}

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
VERSION="$(cat "$(dirname "$SCRIPT_PATH")/VERSION" 2>/dev/null || echo 1.0.0)"
CACHE_DIR="$CACHE_DIR"
mkdir -p "$CACHE_DIR"
CPU_TEMP_FILE="$(find_cpu_temp_file || true)"
[ -n "${CPU_TEMP_FILE:-}" ] || die "не найден датчик температуры CPU (hwmon coretemp/thermal x86_pkg_temp/B0D4)"

case "${1:-}" in
    status)      shift; cmd_status "$@" ;;
    watch)       shift; cmd_watch "$@" ;;
    cpu-quiet-apply) shift; cmd_cpu_quiet apply "$@" ;;
    cpu-quiet)   shift; cmd_cpu_quiet "$@" ;;
    fan-curve)   shift; cmd_fan_curve "$@" ;;
    fan-speed)   shift; cmd_fan_speed "$@" ;;
    fan-auto)    shift; cmd_fan_auto "$@" ;;
    install-nbfc) print_nbfc_install_steps ;;
    unit)        print_unit ;;
    unit-cpuquiet) print_unit_cpuquiet ;;
    unit-timer)  print_unit_timer ;;
    cpu-quiet-assert) shift; cmd_cpu_quiet assert "$@" ;;
    --version|-V) echo "nitro-fan ${VERSION:-1.0.0}"; exit 0 ;;
    ec-prep)     cmd_ec_pre ;;
    ec-read)     cmd_ec_read ;;
    ec-test)     shift; cmd_ec_test "$@" ;;
    ec-max)      cmd_ec_max ;;
    ec-sweep)    cmd_ec_sweep ;;
    *) cat <<EOF
Использование: $0 <команда>

только мониторинг (без root):
  status              Температуры и текущие настройки
  watch [сек]         Повторять status (по умолчанию 3 сек)

CPU-тишина (работает уже сейчас, нужен root):
  cpu-quiet on [МГц]  EPP=balance_power + потолок частоты (по умолчанию 2600000)
  cpu-quiet off       Восстановить прежние значения

Управление вентиляторами (требует NBFC-Linux и активного сервиса):
  fan-speed <0..100>  Задать скорость вентиляторам NBFC
  fan-auto            Вернуть автоматический режим NBFC
  fan-curve           Демон тихой кривой с защитой и возвратом в auto

Справка/автоматизация:
  cpu-quiet-apply [МГц]  Не-интерактивное применение тихих настроек (для автозагрузки)
  cpu-quiet-assert    Перезаписать тихие значения, если nitro-cpu-quiet enable (для таймера)
  install-nbfc        Пошаговая инструкция установки NBFC-Linux
  unit                systemd unit для автозапуска fan-curve
  unit-timer          Юнит re-assert (service+timer) против дрейфа EPP
  --version           Версия скрипта
  unit                systemd unit для автозапуска fan-curve
  ec-prep             Загрузить ec_sys (только чтение) — нужен root
  ec-read             Прочитать EC-регистры вентиляторов и сверить раскладку
  ec-test [сек]       Контролируемый стоп-тест вентиляторов (по умолчанию 90 с)
  ec-max              FAST 5-секундный пинок 100% для проверки пути записи
  ec-sweep            Лестница duty 40→20→10→5→1→0 — минимум прошивки
EOF
    ;;
esac
