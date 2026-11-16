#!/data/data/com.termux/files/usr/bin/bash

echo "Limpiando logs..."
echo

RUTAS=(
  "$HOME/.local/share"
  "$HOME/.cache"
  "/sdcard/Android/media"
  "/sdcard/Download"
)

for r in "${RUTAS[@]}"; do
  if [ -d "$r" ]; then
    echo "🧽 Revisando: $r"
    find "$r" -type f -name "*.log" -o -name "*.txt" 2>/dev/null \
      | grep -i log \
      | xargs -r rm -f
  fi
done

echo
echo "✔️ Logs eliminados."
