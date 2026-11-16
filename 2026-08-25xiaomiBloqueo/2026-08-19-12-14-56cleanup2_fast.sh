#!/data/data/com.termux/files/usr/bin/bash

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/xiaomi_diag_$TIMESTAMP"

mkdir -p "$OUTPUT_DIR"

echo "╔══════════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO RÁPIDO                        ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Directorio: $OUTPUT_DIR                    ║"
echo "╚══════════════════════════════════════════════╝"

# Verificar ADB
adb devices | grep -q "device" || {
    echo "Conectando ADB..."
    adb connect 192.168.0.11:38571
    sleep 2
}

echo ""
echo "[1/7] Almacenamiento..."
adb shell "df -h" > "$OUTPUT_DIR/storage.txt" 2>&1
head -10 "$OUTPUT_DIR/storage.txt"

echo ""
echo "[2/7] Memoria RAM..."
adb shell "cat /proc/meminfo | head -20" > "$OUTPUT_DIR/memory.txt" 2>&1
head -10 "$OUTPUT_DIR/memory.txt"

echo ""
echo "[3/7] SWAP..."
adb shell "grep -E '^Swap' /proc/meminfo" > "$OUTPUT_DIR/swap.txt" 2>&1
cat "$OUTPUT_DIR/swap.txt"

echo ""
echo "[4/7] Sensores térmicos (primeros 20)..."
for i in $(seq 0 19); do
    type=$(adb shell "cat /sys/class/thermal/thermal_zone${i}/type 2>/dev/null" | tr -d '\r' 2>/dev/null)
    temp=$(adb shell "cat /sys/class/thermal/thermal_zone${i}/temp 2>/dev/null" | tr -d '\r' 2>/dev/null)
    [ -n "$type" ] && [ "$type" != "" ] && echo "$type: ${temp}m°C ($((temp/1000))°C)" >> "$OUTPUT_DIR/thermal.txt"
done
head -10 "$OUTPUT_DIR/thermal.txt"

echo ""
echo "[5/7] Batería..."
adb shell "dumpsys battery" > "$OUTPUT_DIR/battery.txt" 2>&1
adb shell "getprop | grep battery" >> "$OUTPUT_DIR/battery.txt" 2>&1
cat "$OUTPUT_DIR/battery.txt"

echo ""
echo "[6/7] Uptime..."
adb shell "cat /proc/uptime" > "$OUTPUT_DIR/uptime.txt" 2>&1
cat "$OUTPUT_DIR/uptime.txt"

echo ""
echo "[7/7] Procesos (TOP 15)..."
adb shell "ps -A --sort=-rss | head -15" > "$OUTPUT_DIR/processes.txt" 2>&1
cat "$OUTPUT_DIR/processes.txt"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO COMPLETADO                    ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Archivos:                                  ║"
ls "$OUTPUT_DIR"
echo "╚══════════════════════════════════════════════╝"

exit 0
