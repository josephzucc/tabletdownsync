#!/data/data/com.termux/files/usr/bin/bash

# ════════════════════════════════════════════
# SCRIPT DEFINITIVO DE DIAGNÓSTICO - XIAOMI 10T LITE 5G
# Versión final corregida (sin errores de permisos)
# ════════════════════════════════════════════

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/xiaomi_final_diagnostic_$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"

IP_XIAOMI="192.168.0.11:38571"

echo "╔══════════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO DEFINITIVO - XIAOMI 10T LITE   ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Inicio: $TIMESTAMP                           ║"
echo "║  IP: $IP_XIAOMI                              ║"
echo "╚══════════════════════════════════════════════╝"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [0] VERIFICAR CONEXIÓN ADB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[0/15] Verificando conexión ADB..."

CONNECTED=$(adb devices 2>&1 | grep -c "device")
if [ "$CONNECTED" -eq 0 ]; then
    echo "🔄 Conectando a $IP_XIAOMI..."
    adb connect $IP_XIAOMI > /dev/null 2>&1
    sleep 3
fi

CONNECTED=$(adb devices 2>&1 | grep -c "device")
if [ "$CONNECTED" -eq 0 ]; then
    echo "❌ ERROR: No se pudo conectar ADB"
    exit 1
fi
echo "✅ ADB conectado"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [1] IDENTIFICACIÓN DEL DISPOSITIVO
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[1/15] Identificación del dispositivo..."

{
    echo "=== IDENTIFICACIÓN DEL DISPOSITIVO ==="
    adb shell "getprop ro.product.model"
    adb shell "getprop ro.product.brand"
    adb shell "getprop ro.build.version.release"
    adb shell "getprop ro.build.version.sdk"
    adb shell "getprop ro.bootloader"
    adb shell "getprop ro.boot.verifiedbootstate"
    adb shell "getprop ro.bootimage.build.fingerprint"
    adb shell "getprop ro.build.display.id"
    adb shell "uname -a"
} > "$OUTPUT_DIR/device_info.txt"

cat "$OUTPUT_DIR/device_info.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [2] ESTADO DEL SISTEMA DE ARCHIVOS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[2/15] Estado del sistema de archivos..."

adb shell "df -h" > "$OUTPUT_DIR/storage_full.txt"
adb shell "df -h | grep '100%'" > "$OUTPUT_DIR/partitions_100_percent.txt"
adb shell "df -h /data" > "$OUTPUT_DIR/data_partition.txt"

echo "=== PARTICIONES 100% (CRÍTICAS) ==="
cat "$OUTPUT_DIR/partitions_100_percent.txt"

echo ""
echo "=== ESPACIO EN /data ==="
cat "$OUTPUT_DIR/data_partition.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [3] MEMORIA RAM Y SWAP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[3/15] Memoria RAM..."

adb shell "free -h" > "$OUTPUT_DIR/memory.txt"
adb shell "grep -E '^Mem:|^Swap:' /proc/meminfo" >> "$OUTPUT_DIR/memory.txt"

cat "$OUTPUT_DIR/memory.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [4] TIEMPO DE ACTIVIDAD (UPTIME)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[4/15] Tiempo de actividad..."

adb shell "cat /proc/uptime" > "$OUTPUT_DIR/uptime.txt"
adb shell "uptime" >> "$OUTPUT_DIR/uptime.txt"
adb shell "date" > "$OUTPUT_DIR/timestamp.txt"

echo "Uptime actual:"
cat "$OUTPUT_DIR/uptime.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [5] ESTADO DE BATERÍA Y TEMPERATURA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[5/15] Batería y temperatura..."

{
    echo "=== BATERÍA ==="
    adb shell "dumpsys battery" 2>/dev/null | head -30
    adb shell "getprop | grep battery"
    
    echo ""
    echo "=== TEMPERATURAS ==="
    for i in $(seq 0 95); do
        type=$(adb shell "cat /sys/class/thermal/thermal_zone${i}/type 2>/dev/null" | tr -d '\r' 2>/dev/null)
        temp=$(adb shell "cat /sys/class/thermal/thermal_zone${i}/temp 2>/dev/null" | tr -d '\r' 2>/dev/null)
        if [ -n "$type" ] && [ "$type" != "" ] && [ "$temp" != "" ]; then
            echo "$type: ${temp}m°C ($((temp/1000))°C)"
        fi
    done | head -30
} > "$OUTPUT_DIR/battery_temp.txt"

head -50 "$OUTPUT_DIR/battery_temp.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [6] PROCESOS ACTIVOS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[6/15] Procesos activos..."

adb shell "ps -A --sort=-rss | head -20" > "$OUTPUT_DIR/top_processes
