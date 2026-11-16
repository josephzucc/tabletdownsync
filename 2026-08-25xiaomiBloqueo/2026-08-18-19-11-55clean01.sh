#!/data/data/com.termux/files/usr/bin/bash

echo "🧹 Limpieza basada en archivos detectados..."
echo

##############################
# 1. Basura de MEGA detectada
##############################

MEGA_DEBRIS="/storage/emulated/0/Download/SubidasCamaraMovil/.debris/tmp"

if [ -d "$MEGA_DEBRIS" ]; then
  echo "🗑️ Eliminando basura de MEGA en: $MEGA_DEBRIS"
  rm -f "$MEGA_DEBRIS"/*.mega 2>/dev/null
else
  echo "⚠️ No existe carpeta de MEGA: $MEGA_DEBRIS"
fi

##############################
# 2. Papelera de Huawei
##############################

HUAWEI_RECYCLE="/storage/emulated/0/.File_Recycle"

if [ -d "$HUAWEI_RECYCLE" ]; then
  echo "🗑️ Vaciando papelera de Huawei: $HUAWEI_RECYCLE"
  rm -rf "$HUAWEI_RECYCLE"/* 2>/dev/null
else
  echo "⚠️ No existe papelera Huawei"
fi

##############################
# 3. Limpieza de caches pesadas en Termux
##############################

RUTAS_TERMUX=(
  "$HOME/.cpan"
  "$HOME/.wdm"
  "$HOME/.cargo/registry"
  "$HOME/.mozilla/firefox"
  "$HOME/Public/filex-termux/node_modules"
)

for r in "${RUTAS_TERMUX[@]}"; do
  if [ -d "$r" ]; then
    echo "🧽 Limpiando: $r"
    rm -rf "$r"/* 2>/dev/null
  else
    echo "⚠️ No existe: $r"
  fi
done

##############################
# 4. Limpieza de thumbnails
##############################

THUMB_DIRS=(
  "/storage/emulated/0/DCIM/.thumbnails"
  "/storage/emulated/0/Pictures/.thumbnails"
)

for t in "${THUMB_DIRS[@]}"; do
  if [ -d "$t" ]; then
    echo "🧽 Eliminando thumbnails en: $t"
    rm -f "$t"/* 2>/dev/null
  fi
done

echo
echo "✔️ Limpieza completada."
