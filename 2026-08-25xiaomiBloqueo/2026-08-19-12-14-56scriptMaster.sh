#!/data/data/com.termux/files/usr/bin/bash

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BASE_DIR="$HOME/xiaomi_master_$TIMESTAMP"

mkdir -p "$BASE_DIR"

IP_XIAOMI="192.168.0.11:38571"

echo "╔══════════════════════════════════════════════╗"
echo "║  SCRIPT MASTER - Xiaomi 10T LITE           ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Inicio: $TIMESTAMP                           ║"
echo "║  Directorio: $BASE_DIR                        ║"
echo "╚══════════════════════════════════════════════╝"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PASO 0: Verificar/conectar ADB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[0/5] Verificando ADB..."
adb devices | grep -q "device" && echo "✅ Ya conectado" || {
    echo "🔄 Conectando..."
    adb connect $IP_XIAOMI
    sleep 3
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PASO 1: Estado inicial
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[1/5] Estado inicial..."

echo "Hora: $(date)" > "$BASE_DIR/initial.txt"
adb shell "cat /proc/uptime" >> "$BASE_DIR/initial.txt"
adb shell "df -h" >> "$BASE_DIR/initial.txt"
adb shell "free -h" >> "$BASE_DIR/initial.txt"

echo "✅ Capturado"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PASO 2: Limpieza
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[2/5] Limpiando..."

echo "Trim: $(date)" > "$BASE_DIR/cleanup_log.txt"
adb shell "pm trim-caches 2147483648" >> "$BASE_DIR/cleanup_log.txt" 2>&1
sleep 2

echo "Logs: $(date)" >> "$BASE_DIR/cleanup_log.txt"
adb shell "rm -rf /data/log/* 2>/dev/null" >> "$BASE_DIR/cleanup_log.txt" 2>&1
adb shell "rm -rf /data/anr/* 2>/dev/null" >> "$BASE_DIR/cleanup_log.txt" 2>&1

sleep 2
echo "✅ Limpieza completada"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PASO 3: Estado final
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[3/5] Estado final..."

echo "Hora: $(date)" > "$BASE_DIR/final.txt"
adb shell "cat /proc/uptime" >> "$BASE_DIR/final.txt"
adb shell "df -h" >> "$BASE_DIR/final.txt"
adb shell "free -h" >> "$BASE_DIR/final.txt"

echo "✅ Capturado"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PASO 4: Análisis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[4/5] Analizando..."

up_initial=$(grep -oE '[0-9]+\.[0-9]+' "$BASE_DIR/initial.txt" | head -1)
up_final=$(grep -oE '[0-9]+\.[0-9]+' "$BASE_DIR/final.txt" | head -1)

echo "Uptime inicial: ${up_initial}s" > "$BASE_DIR/analysis.txt"
echo "Uptime final: ${up_final}s" >> "$BASE_DIR/analysis.txt"

# Verificar si hubo reinicio (uptime = 0 o muy bajo)
up_final_int=$(echo "$up_final" | cut -d. -f1)

if [ "$up_final_int" -lt 30 ] && [ "$up_initial_int" != "$up_final_int" ]; then
    echo "" >> "$BASE_DIR/analysis.txt"
    echo "⚠️  POSIBLE REINICIO: Uptime muy bajo (<30s)" >> "$BASE_DIR/analysis.txt"
else
    echo "" >> "$BASE_DIR/analysis.txt"
    echo "✅ SISTEMA ESTABLE (sin reinicios durante ejecución)" >> "$BASE_DIR/analysis.txt"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PASO 5: Reporte
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[5/5] Generando reporte..."

echo "" > "$BASE_DIR/report.txt"
echo "╔══════════════════════════════════════════╗" >> "$BASE_DIR/report.txt"
echo "║  REPORTE FINAL                         ║" >> "$BASE_DIR/report.txt"
echo "╚══════════════════════════════════════════╝" >> "$BASE_DIR/report.txt"
echo "" >> "$BASE_DIR/report.txt"
cat "$BASE_DIR/analysis.txt" >> "$BASE_DIR/report.txt"
echo "" >> "$BASE_DIR/report.txt"
echo "Archivos generados:" >> "$BASE_DIR/report.txt"
ls "$BASE_DIR" >> "$BASE_DIR/report.txt"

cat "$BASE_DIR/report.txt"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  COMPLETADO                                ║"
echo "╠══════════════════════════════════════════════╣"
ls "$BASE_DIR"
echo "╚══════════════════════════════════════════════╝"

exit 0
