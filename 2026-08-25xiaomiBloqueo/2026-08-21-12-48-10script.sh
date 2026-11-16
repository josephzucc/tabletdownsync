#!/data/data/com.termux/files/usr/bin/bash

# ════════════════════════════════════════════
# SCRIPT MASTER CON CAPTURA DE ERRORES
# Guarda cada error en archivos separados
# ════════════════════════════════════════════

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/master_diagnostic_$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"
IP_XIAOMI="192.168.0.11:38571"

# Función helper para ejecutar comandos y capturar errores
run_command() {
    local desc="$1"
    local cmd="$2"
    local outfile="$3"

    echo "[$desc] Ejecutando..." >> "$OUTPUT_DIR/main_log.txt"
    eval "$cmd" > "$outfile" 2>> "$OUTPUT_DIR/errors.txt"

    if [ $? -eq 0 ]; then
        echo "  ✅ Éxito" >> "$OUTPUT_DIR/main_log.txt"
    else
        echo "  ❌ Error (ver errors.txt)" >> "$OUTPUT_DIR/main_log.txt"
    fi
}

echo "╔══════════════════════════════════════════════╗"
echo "║  SCRIPT MASTER CON MANEJO DE ERRORES       ║"
echo "╚══════════════════════════════════════════════╝"
echo "Fecha: $TIMESTAMP"
echo "Directorio: $OUTPUT_DIR"

# Inicializar archivos de log
echo "=== LOG PRINCIPAL ===" > "$OUTPUT_DIR/main_log.txt"
echo "Fecha: $TIMESTAMP" >> "$OUTPUT_DIR/main_log.txt"
echo "" > "$OUTPUT_DIR/errors.txt"
echo "=== ERROR DETALLE ===" > "$OUTPUT_DIR/error_details.txt"
echo "=== COMANDOS FALLIDOS ===" > "$OUTPUT_DIR/failed_commands.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [0] CONEXIÓN ADB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[0/20] Conectando ADB..."
adb devices > "$OUTPUT_DIR/adb_devices.txt" 2>&1

if ! adb devices 2>&1 | grep -q "device"; then
    echo "Intentando conectar..." >> "$OUTPUT_DIR/main_log.txt"
    adb connect $IP_XIAOMI > "$OUTPUT_DIR/adb_connect.log" 2>&1
    sleep 3

    if ! adb devices 2>&1 | grep -q "device"; then
        echo "❌ ERROR FATAL: ADB no conectado" >> "$OUTPUT_DIR/main_log.txt"
        echo "Comando fallido: adb connect $IP_XIAOMI" >> "$OUTPUT_DIR/failed_commands.txt"
        cat "$OUTPUT_DIR/adb_connect.log" >> "$OUTPUT_DIR/error_details.txt"
        exit 1
    fi
fi
echo "✅ ADB conectado" >> "$OUTPUT_DIR/main_log.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [1-20] COMANDOS DIAGNÓSTICOS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo "[1/20] Información dispositivo..."
run_command "1" "adb shell 'getprop ro.product.model; getprop ro.build.version.release; getprop ro.bootloader'" "$OUTPUT_DIR/device_id.txt"

echo "[2/20] Almacenamiento..."
run_command "2" "adb shell 'df -h'" "$OUTPUT_DIR/storage_full.txt"
run_command "2b" "adb shell 'df -h | grep 100%'" "$OUTPUT_DIR/partitions_100pct.txt"

echo "[3/20] Memoria RAM..."
run_command "3" "adb shell 'free -h'" "$OUTPUT_DIR/memory.txt"

echo "[4/20] Uptime..."
run_command "4" "adb shell 'cat /proc/uptime; date'" "$OUTPUT_DIR/uptime.txt"

echo "[5/20] Batería (método 1)..."
run_command "5a" "adb shell 'dumpsys battery'" "$OUTPUT_DIR/battery_dumpsys.txt"
run_command "5b" "adb shell 'getprop | grep battery'" "$OUTPUT_DIR/battery_props.txt"

echo "[6/20] Temperaturas..."
{
    echo "=== ZONAS TÉRMICAS ===" 
    for i in $(seq 0 95); do
        type=$(adb shell "cat /sys/class/thermal/thermal_zone${i}/type 2>/dev/null" | tr -d '\r')
        temp=$(adb shell "cat /sys/class/thermal/thermal_zone${i}/temp 2>/dev/null" | tr -d '\r')
        if [ -n "$type" ] && [ "$type" != "" ]; then
            echo "$type: ${temp}m°C ($((temp/1000))°C)"
        fi
    done
} > "$OUTPUT_DIR/thermal_zones.txt" 2>> "$OUTPUT_DIR/errors.txt"

echo "[7/20] Procesos activos..."
run_command "7" "adb shell 'ps -A --sort=-rss | head -20'" "$OUTPUT_DIR/top_processes.txt"

echo "[8/20] Aplicaciones instaladas..."
run_command "8a" "adb shell 'pm list packages -3 | wc -l'" "$OUTPUT_DIR/app_count.txt"
run_command "8b" "adb shell 'pm list packages -3'" "$OUTPUT_DIR/third_party_apps.txt"

echo "[9/20] Espacio /data detalle..."
run_command "9" "adb shell 'du -sh /data/* 2>/dev/null | sort -hr | head -20'" "$OUTPUT_DIR/data_usage.txt"

echo "[10/20] Caché de aplicaciones..."
run_command "10a" "adb shell 'du -sh /data/*/cache 2>/dev/null | sort -hr | head -15'" "$OUTPUT_DIR/app_cache.txt"

echo "[11/20] Servicios de autenticación..."
run_command "11a" "adb shell 'service list | grep -i lock'" "$OUTPUT_DIR/auth_services.txt"
run_command "11b" "adb shell 'service list | grep -i auth'" >> "$OUTPUT_DIR/auth_services.txt" 2>&1

echo "[12/20] Propiedades de sistema..."
run_command "12" "adb shell 'getprop'" "$OUTPUT_DIR/all_properties.txt"

echo "[13/20] Estado cifrado..."
run_command "13" "adb shell 'getprop | grep crypto'" "$OUTPUT_DIR/crypto_state.txt"

echo "[14/20] Logs logcat (errores)..."
run_command "14a" "adb shell 'logcat -d -t 50 *:E'" "$OUTPUT_DIR/logcat_errors.txt"
run_command "14b" "adb shell 'logcat -d -t 50 | grep -iE reboot|shutdown|panic' " "$OUTPUT_DIR/logcat_reboot.txt"

echo "[15/20] Archivos tombstones (crashes)..."
run_command "15" "adb shell 'ls -la /data/tombstones/ 2>/dev/null'" "$OUTPUT_DIR/tombstones.txt"

echo "[16/20] Estado SELinux..."
run_command "16a" "adb shell 'getprop ro.build.type'" "$OUTPUT_DIR/build_type.txt"
run_command "16b" "adb shell 'getprop ro.boot.selinux'" >> "$OUTPUT_DIR/build_type.txt" 2>&1

echo "[17/20] Historial de reboots..."
run_command "17a" "adb shell 'cat /proc/sys/kernel/boot_reason'" "$OUTPUT_DIR/boot_reason.txt"
run_command "17b" "adb shell 'uptime -s 2>/dev/null'" >> "$OUTPUT_DIR/boot_reason.txt" 2>&1

echo "[18/20] Procesos del kernel..."
run_command "18" "adb shell 'ps -A | grep kernel'" "$OUTPUT_DIR/kernel_processes.txt"

echo "[19/20] Estado WiFi y red..."
run_command "19a" "adb shell 'dumpsys connectivity'" "$OUTPUT_DIR/network_state.txt"

echo "[20/20] Resumen final..."

# Generar resumen automático
{
    echo "╔══════════════════════════════════════════════╗"
    echo "║  RESUMEN AUTOMÁTICO                        ║"
    echo "╚══════════════════════════════════════════════╝"
    echo ""
    echo "=== ESTADO GENERAL ==="
    cat "$OUTPUT_DIR/uptime.txt"
    echo ""
    echo "=== PARTICIONES 100% ==="
    cat "$OUTPUT_DIR/partitions_100pct.txt"
    echo ""
    echo "=== MEMORIA RAM ==="
    cat "$OUTPUT_DIR/memory.txt"
    echo ""
    echo "=== BATERÍA ==="
    head -10 "$OUTPUT_DIR/battery_dumpsys.txt"
    echo ""
    echo "=== ERRORES DETECTADOS ==="
    error_count=$(wc -l < "$OUTPUT_DIR/errors.txt" 2>/dev/null || echo 0)
    echo "Total líneas de error: $error_count"
    if [ "$error_count" -gt 0 ]; then
        echo ""
        echo "Últimos 20 errores:"
        tail -20 "$OUTPUT_DIR/errors.txt"
    fi
} > "$OUTPUT_DIR/auto_summary.txt"

cat "$OUTPUT_DIR/auto_summary.txt"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO COMPLETADO                    ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  ARCHIVOS GENERADOS:                       ║"
ls -la "$OUTPUT_DIR"
echo "║                                            ║"
echo "║  ARCHIVOS CRÍTICOS PARA SUBIR:             ║"
echo "║  1. auto_summary.txt                       ║"
echo "║  2. errors.txt                             ║"
echo "║  3. error_details.txt                      ║"
echo "║  4. failed_commands.txt                    ║"
echo "║  5. partitions_100pct.txt                  ║"
echo "╚══════════════════════════════════════════════╝"

exit 0


