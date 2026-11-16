#!/data/data/com.termux/files/usr/bin/bash

# ════════════════════════════════════════════
# DIAGNÓSTICO AUTH/REBOOT - XIAOMI 10T LITE
# Versión final para captura de logs críticos
# ════════════════════════════════════════════

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/auth_diagnostic_$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"

IP_XIAOMI="192.168.0.11:38571"

echo "╔══════════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO AUTH/REBOOT                   ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Inicio: $TIMESTAMP                           ║"
echo "║  IP: $IP_XIAOMI                              ║"
echo "╚══════════════════════════════════════════════╝"

# Verificar conexión
adb devices | grep -q "device" || adb connect $IP_XIAOMI
sleep 2

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [1] STATE INICIAL - ANTES DE CUALQUIER ACCIÓN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[1/8] Estado inicial..."

adb shell "date > $OUTPUT_DIR/timestamp_start.txt"
adb shell "cat /proc/uptime >> $OUTPUT_DIR/timestamp_start.txt"
adb shell "cat /proc/meminfo | head -20 >> $OUTPUT_DIR/timestamp_start.txt"
adb shell "df -h >> $OUTPUT_DIR/timestamp_start.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [2] SELINUX VIOLATIONS - CAUSA PROBABLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[2/8] Violaciones SELinux..."

adb shell "dmesg | grep -i 'avc: denied' | tail -100" > "$OUTPUT_DIR/selinux_denials.txt" 2>&1
adb shell "grep 'avc: denied' /proc/last_kmsg 2>/dev/null" >> "$OUTPUT_DIR/selinux_denials.txt" 2>&1
adb shell "logcat -d | grep -i 'selinux\|denied\|permission' | tail -200" >> "$OUTPUT_DIR/selinux_logcat.txt" 2>&1

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [3] KERNEL LOGS DE REBOOT/CRASH
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[3/8] Logs kernel recientes..."

adb shell "dmesg | grep -iE 'reboot|shutdown|panic|oom|kill|fatal' | tail -100" > "$KERNEL_REBOOT_LOGS"
adb shell "dmesg | tail -500" > "$OUTPUT_DIR/dmesg_recent.txt"

# Último reboot reason
adb shell "getprop | grep -i reboot" > "$OUTPUT_DIR/reboot_reason.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [4] ESTADO DE CRIPTOGRAFÍA / DATA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[4/8] Estado cifrado y /data..."

adb shell "dumpsys deviceidle | head -30" > "$OUTPUT_DIR/encryption.txt" 2>&1
adb shell "settings get secure encryption_enabled 2>/dev/null" >> "$OUTPUT_DIR/encryption.txt"
adb shell "getprop | grep -i encrypt" >> "$OUTPUT_DIR/encryption.txt"

# Estado filesystem
adb shell "cat /sys/fs/ext4/*/errors_cnt 2>/dev/null" > "$OUTPUT_DIR/fs_errors.txt"
adb shell "dmesg | grep -i 'ext4\|filesystem\|corrupt' | tail -50" >> "$OUTPUT_DIR/fs_errors.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [5] PROCESOS DE AUTHENTICATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[5/8] Procesos authentication..."

adb shell "ps -A | grep -iE 'lock|auth|credential|keystore'" > "$OUTPUT_DIR/auth_processes.txt"
adb shell "service list | grep -iE 'lock|auth|credential'" >> "$OUTPUT_DIR/auth_services.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [6] MEMORY TRACES TRAS FAILURES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[6/8] Traces de memoria..."

adb shell "cat /sys/fs/cgroup/memory/memory.oom_control 2>/dev/null" > "$OUTPUT_DIR/oom_control.txt"
adb shell "grep -r 'OOM' /proc/*/oom_score_adj 2>/dev/null | head -20" >> "$OUTPUT_DIR/oom_traces.txt"

# Tombstones (crash dumps del sistema)
adb shell "ls -la /data/tombstones/ 2>/dev/null" > "$OUTPUT_DIR/tombstones.txt"
adb shell "tail -50 /data/tombstones/tombstone_*.0 2>/dev/null" >> "$OUTPUT_DIR/latest_tombstone.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [7] BOOT ANTECEDENTES (ÚLTIMOS 5 BOOTS)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[7/8] Historial de reboots..."

adb shell "cat /proc/sys/kernel/boot_reason 2>/dev/null" > "$OUTPUT_DIR/boot_reason.txt"
adb shell "cat /sys/fs/cgroup/memory/memory.oom_kill_count 2>/dev/null" >> "$OUTPUT_DIR/boot_reason.txt"

# Timestamps de boot
adb shell "uptime -s 2>/dev/null" >> "$OUTPUT_DIR/boot_times.txt"
adb shell "who -b 2>/dev/null" >> "$OUTPUT_DIR/boot_times.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [8] ANÁLISIS AUTOMÁTICO
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[8/8] Generando análisis..."

{
    echo "╔══════════════════════════════════════════════╗"
    echo "║  ANÁLISIS AUTOMÁTICO                      ║"
    echo "╚══════════════════════════════════════════════╝"
    echo ""
    echo "=== ESTADO INICIAL ==="
    cat "$OUTPUT_DIR/timestamp_start.txt"

    echo ""
    echo "=== PARTICIONES 100% (CRÍTICAS) ==="
    adb shell "df -h | grep '100%' || echo 'Ninguna partición al 100% ahora'"

    echo ""
    echo "=== SELINUX DENIALS (TOP 10) ==="
    head -10 "$OUTPUT_DIR/selinux_denials.txt"

    echo ""
    echo "=== FILESYSTEM ERRORS ==="
    cat "$OUTPUT_DIR/fs_errors.txt"

    echo ""
    echo "=== TOMBSTONES (CRASHES) ==="
    ls -la /data/tombstones/ 2>/dev/null || echo "No tombstones encontrados"

    echo ""
    echo "=== BOOT REASON ==="
    cat "$OUTPUT_DIR/boot_reason.txt"

    echo ""
    echo "=== RESUMEN ==="

    # Detectar patrón
    selinux_count=$(wc -l < "$OUTPUT_DIR/selinux_denials.txt" 2>/dev/null || echo 0)
    if [ "$selinux_count" -gt 10 ]; then
        echo "⚠️  Múltiples violaciones SELinux (${selinux_count} encontradas)"
        echo "   → Posible política de seguridad bloqueando acceso"
    fi

    fs_error_count=$(grep -c "error\|corrupt" "$OUTPUT_DIR/fs_errors.txt" 2>/dev/null || echo 0)
    if [ "$fs_error_count" -gt 0 ]; then
        echo "⚠️  Errores de filesystem detectados (${fs_error_count} referencias)"
        echo "   → Corrupción posible en /data o particiones del sistema"
    fi

    oom_count=$(grep -c "OOM" "$OUTPUT_DIR/oom_control.txt" 2>/dev/null || echo 0)
    if [ "$oom_count" -gt 0 ]; then
        echo "⚠️  Kill OOM frecuentes detectados"
        echo "   → Memoria insuficiente matando procesos críticos"
    fi

    # Recomendación final
    echo ""
    echo "=== RECOMENDACIÓN ==="
    echo "Basándome en el análisis:"
    echo ""
    echo "Si ves múltiples errores de filesystem → Factory Reset"
    echo "Si ves muchas violaciones SELinux → Boot recovery + restore permissions"
    echo "Si ves OOM kills → Liberar RAM + Factory Reset"

} > "$OUTPUT_DIR/analysis_final.txt"

cat "$OUTPUT_DIR/analysis_final.txt"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO COMPLETADO                    ║"
echo "╠══════════════════════════════════════════════╣"
ls -la "$OUTPUT_DIR"
echo "╚══════════════════════════════════════════════╝"

exit 0
