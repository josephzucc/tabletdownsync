#!/data/data/com.termux/files/usr/bin/bash

# ════════════════════════════════════════════
# VERSIÓN CORREGIDA - Directorio definido antes
# ════════════════════════════════════════════

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/xiaomi_cleanup_$TIMESTAMP"

# CREAR DIRECTORIO PRIMERO (CRÍTICO)
mkdir -p "$OUTPUT_DIR"

echo "╔══════════════════════════════════════════════╗"
echo "║  LIMPIEZA RÁPIDA - XIAOMI 10T LITE          ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Directorio: $OUTPUT_DIR                    ║"
echo "╚══════════════════════════════════════════════╝"

# Verificar conexión 

echo ""
echo "[0/8] Verificando conexión ADB..."
CONNECTED=$(adb devices 2>&1 | grep -c "device")
if [ "$CONNECTED" -eq 0 ]; then
    echo "🔄 Conectando a 192.168.0.11:38571..."
    adb connect 192.168.0.11:38571 > /dev/null 2>&1
    sleep 2
    CONNECTED=$(adb devices 2>&1 | grep -c "device")
    if [ "$CONNECTED" -eq 0 ]; then
        echo "❌ ERROR: ADB no conectado"
        exit 1
    fi
fi
echo "✅ ADB conectado"

# Guardar estado inicial
echo ""
echo "[1/8] Estado inicial de almacenamiento..."
adb shell "df -h" > "$OUTPUT_DIR/storage_before.txt" 2>&1
cat "$OUTPUT_DIR/storage_before.txt"

# Trim cache
echo ""
echo "[2/8] Limpiando cache de aplicaciones..."
adb shell "pm trim-caches 2147483648" > "$OUTPUT_DIR/trim_result.txt" 2>&1
echo "Trim enviado: $(date)" >> "$OUTPUT_DIR/cleanup_log.txt"

# Borrar logs
echo ""
echo "[3/8] Borrando logs antiguos..."
adb shell "rm -rf /data/log/* 2>/dev/null; rm -rf /data/anr/* 2>/dev/null; rm -rf /data/tombstones/* 2>/dev/null" > "$OUTPUT_DIR/logs_removed.txt" 2>&1
echo "Logs borrados: $(date)" >> "$OUTPUT_DIR/cleanup_log.txt"

# Reiniciar servicios
echo ""
echo "[4/8] Reiniciando servicios del sistema..."
adb shell "am kill --user 0 :* 2>/dev/null" >> "$OUTPUT_DIR/cleanup_log.txt" 2>&1
echo "Services killed: $(date)" >> "$OUTPUT_DIR/cleanup_log.txt"
sleep 3

# Estado final
echo ""
echo "[5/8] Estado final de almacenamiento..."
adb shell "df -h" > "$OUTPUT_DIR/storage_after.txt" 2>&1

echo ""
echo "[6/8] Memoria RAM..."
adb shell "free -h" > "$OUTPUT_DIR/memory_after.txt" 2>&1

echo ""
echo "[7/8] Procesos activos..."
adb shell "ps -A --sort=-rss | head -20" > "$OUTPUT_DIR/processes.txt" 2>&1

# Análisis
echo ""
echo "[8/8] Análisis..."
before_size=$(awk '/\/$/ {gsub(/%/,"",$5); print $5}' "$OUTPUT_DIR/storage_before.txt" 2>/dev/null | head -1)
after_size=$(awk '/\/$/ {gsub(/%/,"",$5); print $5}' "$OUTPUT_DIR/storage_after.txt" 2>/dev/null | head -1)

echo "Uso inicial: ${before_size}%" > "$OUTPUT_DIR/analysis.txt"
echo "Uso final: ${after_size}%" >> "$OUTPUT_DIR/analysis.txt"

# Reiniciar servicios
echo ""
echo "[4/8] Reiniciando servicios del sistema..."
adb shell "am kill --user 0 :* 2>/dev/null" >> "$OUTPUT_DIR/cleanup_log.txt" 2>&1
echo "Services killed: $(date)" >> "$OUTPUT_DIR/cleanup_log.txt"
sleep 3

# Estado final
echo ""
echo "[5/8] Estado final de almacenamiento..."
adb shell "df -h" > "$OUTPUT_DIR/storage_after.txt" 2>&1

echo ""
echo "[6/8] Memoria RAM..."
adb shell "free -h" > "$OUTPUT_DIR/memory_after.txt" 2>&1

echo ""
echo "[7/8] Procesos activos..."
adb shell "ps -A --sort=-rss | head -20" > "$OUTPUT_DIR/processes.txt" 2>&1

# Análisis
echo ""
echo "[8/8] Análisis..."
before_size=$(awk '/\/$/ {gsub(/%/,"",$5); print $5}' "$OUTPUT_DIR/storage_before.txt" 2>/dev/null | head -1)
after_size=$(awk '/\/$/ {gsub(/%/,"",$5); print $5}' "$OUTPUT_DIR/storage_after.txt" 2>/dev/null | head -1)

echo "Uso inicial: ${before_size}%" > "$OUTPUT_DIR/analysis.txt"
echo "Uso final: ${after_size}%" >> "$OUTPUT_DIR/analysis.txt"

