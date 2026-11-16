#!/data/data/com.termux/files/usr/bin/bash

echo "=== LIMPIEZA PROFUNDA VÍA ADB ==="

# 1. Limpiar cache de TODAS las apps
echo "[1/5] Trim cache global..."
adb shell "pm trim-caches 4294967295"

# 2. Limpiar Dalvik cache
echo "[2/5] Limpiando Dalvik..."
adb shell "rm -rf /data/dalvik-cache/*" 2>/dev/null

# 3. Borrar logs del sistema
echo "[3/5] Borrando logs..."
adb shell "rm -rf /data/log/* 2>/dev/null; rm -rf /data/anr/* 2>/dev/null; rm -rf /data/tombstones/* 2>/dev/null"

# 4. Limpiar caché de apps individuales
echo "[4/5] Limpieza app por app..."
adb shell "pm list packages | head -20 | while read pkg; do 
  pkg_name=\$(echo \$pkg | cut -d: -f2)
  pm clear --user 0 \$pkg_name 2>/dev/null
done"

# 5. Liberar RAM
echo "[5/5] Reiniciando servicios..."
adb shell "am kill --user 0 :* 2>/dev/null"

echo ""
echo "Verificando espacio liberado..."
adb shell "df -h | grep -E '/ |/data'"

