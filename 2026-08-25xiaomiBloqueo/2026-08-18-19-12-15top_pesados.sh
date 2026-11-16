#!/data/data/com.termux/files/usr/bin/bash

# Directorios accesibles sin root
DIRS=(
  "$HOME"
  "/sdcard"
  "/storage/emulated/0"
)

echo "Buscando archivos grandes..."
echo

for d in "${DIRS[@]}"; do
  if [ -d "$d" ]; then
    echo "📂 Analizando: $d"
    # Lista archivos ordenados por tamaño, mostrando los 30 más grandes
    find "$d" -type f -printf "%s %p\n" 2>/dev/null \
      | sort -nr \
      | head -n 30 \
      | awk '{ printf "%10d KB  %s\n", $1/1024, $2 }'
    echo
  fi
done
