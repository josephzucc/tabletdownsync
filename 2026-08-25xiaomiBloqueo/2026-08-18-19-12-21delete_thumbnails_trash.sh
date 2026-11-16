#!/data/data/com.termux/files/usr/bin/bash

RUTAS=(
  "/sdcard/DCIM/.thumbnails"
  "/sdcard/Pictures/.thumbnails"
  "/sdcard/Android/media"
)

echo "Limpiando thumbnails..."
echo

for r in "${RUTAS[@]}"; do
  if [ -d "$r" ]; then
    echo "🧽 Revisando: $r"
    find "$r" -type f -name "*.jpg" -o -name "*.png" -o -name "*.webp" 2>/dev/null \
      | grep -i thumbnail \
      | xargs -r rm -f
  fi
done

echo
echo "✔️ Thumbnails eliminados."
