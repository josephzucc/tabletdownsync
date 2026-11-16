#!/data/data/com.termux/files/usr/bin/bash

echo "🧹 Limpiando basura de MEGA..."
echo

# Rutas reales de MEGA en Android moderno
RUTAS=(
  "/sdcard/Android/media/mega.privacy.android.app"
  "/sdcard/Android/media/mega.privacy.android.app/MEGA"
  "/sdcard/Android/media/mega.privacy.android.app/MEGA/.trash"
  "/sdcard/Android/media/mega.privacy.android.app/MEGA/debris"
  "/sdcard/MEGA"
  "/sdcard/Download/MEGA"
)

for r in "${RUTAS[@]}"; do
  if [ -d "$r" ]; then
    echo "🗑️ Eliminando contenido de: $r"
    find "$r" -type f -print0 2>/dev/null | xargs -0 -r rm -f
    find "$r" -type d -empty -delete 2>/dev/null
  else
    echo "⚠️ No existe: $r"
  fi
done

echo
echo "✔️ Limpieza de MEGA completada."
