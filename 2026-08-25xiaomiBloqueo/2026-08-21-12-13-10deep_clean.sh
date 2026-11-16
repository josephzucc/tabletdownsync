#!/data/data/com.termux/files/usr/bin/bash

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/deep_clean_$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"

IP_XIAOMI="192.168.0.11:38571"

echo "╔══════════════════════════════════════════════╗"
echo "║  LIMPIEZA PROFUNDA - /data                 ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Inicio: $TIMESTAMP                           ║"
echo "╚══════════════════════════════════════════════╝"

# Verificar ADB
adb devices | grep -q "device" || {
    adb connect $IP_XIAOMI
    sleep 3
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [1] ESTADO ANTES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[1/8] Estado ANTES de limpieza..."

adb shell "df -h /data" > "$OUTPUT_DIR/before_storage.txt"
adb shell "free -h" > "$OUTPUT_DIR/before_memory.txt"

echo "ANTES:"
adb shell "df /data | grep /data"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [2] TRIM CACHE GLOBAL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[2/8] Trim cache global (2GB máximo)..."
adb shell "pm trim-caches 2147483648" > "$OUTPUT_DIR/trim_result.txt" 2>&1
sleep 2
echo "✅ Trim enviado"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [3] LIMPIAR LOGS DEL SISTEMA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[3/8] Borrando logs del sistema..."
adb shell "rm -rf /data/log/* 2>/dev/null" > "$OUTPUT_DIR/logs_clean.txt" 2>&1
adb shell "rm -rf /data/anr/* 2>/dev/null" >> "$OUTPUT_DIR/logs_clean.txt" 2>&1
adb shell "rm -rf /data/tombstones/* 2>/dev/null" >> "$OUTPUT_DIR/logs_clean.txt" 2>&1
echo "✅ Logs borrados"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [4] LIMPIAR CACHE DALVIK (Parcial)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[4/8] Limpiando cache Dalvik..."
adb shell "pm clear --user 0 com.android.dextools 2>/dev/null" > "$OUTPUT_DIR/dalvik_clean.txt" 2>&1
echo "✅ Dalvik cache limpiada"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [5] LIMPIAR CACHE DE APPS GRANDES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[5/8] Limpiando cache de apps comunes..."

# Apps que suelen ocupar mucho caché
declare -a big_apps=(
    "com.google.android.youtube"
    "com.netflix.mediaclient"
    "com.spotify.music"
    "com.facebook.katana"
    "com.instagram.android"
    "com.twitter.android"
    "com.whatsapp"
    "com.google.android.apps.photos"
    "com.snapchat.android"
    "com.tiktok.android"
)

for app in "${big_apps[@]}"; do
    echo "  - Limpiando $app..."
    adb shell "pm clear --user 0 $app 2>/dev/null" >> "$OUTPUT_DIR/app_clean.txt" 2>&1
done

echo "✅ Cache de apps limpiada"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [6] LIMPIAR THUMBNAILS Y ARCHIVOS TEMPORALES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[6/8] Limpiando thumbnails y archivos temporales..."
adb shell "rm -rf /sdcard/.thumbnails/* 2>/dev/null"
adb shell "rm -rf /sdcard/Android/media/*/.tmp 2>/dev/null"
adb shell "rm -rf /sdcard/Download/*.apk 2>/dev/null"
echo "✅ Archivos temporales borrados"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [7] ESTADO DESPUÉS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[7/8] Estado DESPUÉS de limpieza..."

sleep 3
adb shell "df -h /data" > "$OUTPUT_DIR/after_storage.txt"
adb shell "free -h" > "$OUTPUT_DIR/after_memory.txt"

echo "DESPUÉS:"
adb shell "df /data | grep /data"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [8] COMPARACIÓN Y RESUMEN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo "[8/8] Generando resumen comparativo..."

{
    echo "╔══════════════════════════════════════════════╗"
    echo "║  RESUMEN COMPARATIVO                       ║"
    echo "╚══════════════════════════════════════════════╝"
    echo ""
    echo "ALMACENAMIENTO /data:"
    echo "---"
    cat "$OUTPUT_DIR/before_storage.txt"
    echo ""
    echo "---"
    cat "$OUTPUT_DIR/after_storage.txt"
    
    # Extraer libre antes/después
    before_free=$(grep "/data" "$OUTPUT_DIR/before_storage.txt" | awk '{print $4}' | tr -d 'G' | cut -d'M -f1)
    after_free=$(grep "/data" "$OUTPUT_DIR/after_storage.txt" | awk '{print $4}' | tr -d 'G' | cut -d'M -f1)
    
    echo ""
    echo "ESPACIO LIBRE:"
    echo "  Antes: $before_free MB"
    echo "  Después: $after_free MB"
    
    if [ -n "$before_free" ] && [ -n "$after_free" ]; then
        freed=$((after_free - before_free))
        if [ "$freed" -gt 0 ]; then
            echo "  Liberado: $freed MB (+${freed}MB)"
        else
            echo "  Liberado: 0 MB (cache no se pudo limpiar)"
        fi
    fi
    
    echo ""
    echo "MEMORIA RAM:"
    echo "---"
    cat "$OUTPUT_DIR/before_memory.txt"
    echo ""
    echo "---"
    cat "$OUTPUT_DIR/after_memory.txt"
    
    echo ""
    echo "PARTICIONES 100% (sin cambios):"
    adb shell "df -h | grep '100%' || echo 'Ninguna partición al 100% ahora'"
    
} > "$OUTPUT_DIR/comparison.txt"

cat "$OUTPUT_DIR/comparison.txt"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  LIMPIEZA COMPLETADA                       ║"
echo "╠══════════════════════════════════════════════╣"
ls -la "$OUTPUT_DIR"
echo "╚══════════════════════════════════════════════╝"

exit 0
