#!/data/data/com.termux/files/usr/bin/bash

echo "🧹 Limpieza selectiva basada en archivos detectados..."
echo

###############################################
# CONFIGURACIÓN: tamaño mínimo para borrar
###############################################
# Cambia este valor si quieres otro límite
MIN_MB=50
MIN_BYTES=$((MIN_MB * 1024 * 1024))

echo "📏 Borrando archivos mayores a $MIN_MB MB"
echo

###############################################
# 1. Basura de MEGA detectada
###############################################

MEGA_DEBRIS="/storage/emulated/0/Download/SubidasCamaraMovil/.debris/tmp"

if [ -d "$MEGA_DEBRIS" ]; then
  echo "🗑️ MEGA → $MEGA_DEBRIS"
  find "$MEGA_DEBRIS" -type f -size +"$MIN_BYTES"c -print -delete 2>/dev/null
else
  echo "⚠️ No existe carpeta de MEGA: $MEGA_DEBRIS"
fi

###############################################
# 2. Papelera de Huawei
###############################################

HUAWEI_RECYCLE="/storage/emulated/0/.File_Recycle"

if [ -d "$HUAWEI_RECYCLE" ]; then
  echo "🗑️ Papelera Huawei → $HUAWEI_RECYCLE"
  find "$HUAWEI_RECYCLE" -type f -size +"$MIN_BYTES"c -print -delete 2>/dev/null
else
  echo "⚠️ No existe papelera Huawei"
fi

###############################################
# 3. Limpieza selectiva de caches pesadas Termux
###############################################

RUTAS_TERMUX=(
  "$HOME/.cpan"
  "$HOME/.wdm"
  "$HOME/.cargo/registry"
  "$HOME/.mozilla/firefox"
  "$HOME/Public/filex-termux/node_modules"
)

for r in "${RUTAS_TERMUX[@]}"; do
  if [ -d "$r" ]; then
    echo "🧽 Termux → $r"
    find "$r" -type f -size +"$MIN_BYTES"c -print -delete 2>/dev/null
  else
    echo "⚠️ No existe: $r"
  fi
done

###############################################
# 4. Limpieza selectiva de thumbnails
###############################################

THUMB_DIRS=(
  "/storage/emulated/0/DCIM/.thumbnails"
  "/storage/emulated/0/Pictures/.thumbnails"
)

for t in "${THUMB_DIRS[@]}"; do
  if [ -d "$t" ]; then
    echo "🧽 Thumbnails → $t"
    find "$t" -type f -size +"$MIN_BYTES"c -print -delete 2>/dev/null
  fi
done

echo
echo "✔️ Limpieza selectiva completada."
