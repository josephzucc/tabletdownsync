#!/data/data/com.termux/files/usr/bin/bash

OUTPUT_DIR="$HOME/storage_check_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "╔══════════════════════════════════════════════╗"
echo "║  VERIFICACIÓN ESPACIO LIBRE                ║"
echo "╚══════════════════════════════════════════════╝"

IP_XIAOMI="192.168.0.11:38571"

# Verificar ADB
adb devices | grep -q "device" || {
    echo "Conectando..."
    adb connect $IP_XIAOMI
    sleep 3
}

echo ""
echo "=== ALMACENAMIENTO ANTES ==="
adb shell "df -h" > "$OUTPUT_DIR/storage.txt"
cat "$OUTPUT_DIR/storage.txt"

echo ""
echo "=== PARTICIONES 100% (CRÍTICAS) ==="
adb shell "df -h | grep '100%'"

echo ""
echo "=== LIBRE EN /data ==="
adb shell "df /data"

echo ""
echo "=== TOP APLICACIONES POR ESPACIO ==="
adb shell "du -sh /data/app/* 2>/dev/null | sort -hr | head -10"

echo ""
echo "=== APLICACIONES INSTALADAS (CANTIDAD) ==="
adb shell "pm list packages -3 | wc -l"
echo "apps de terceros instaladas"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  COMPLETADO                                ║"
echo "║  Directorio: $OUTPUT_DIR                      ║"
ls -la "$OUTPUT_DIR"
echo "╚══════════════════════════════════════════════╝"

exit 0
