#!/data/data/com.termux/files/usr/bin/bash

echo "📊 Información del almacenamiento del dispositivo"
echo

# Punto de montaje principal del almacenamiento interno
TARGET="/storage/emulated/0"

# Obtener datos con df
INFO=$(df -h "$TARGET" | tail -n 1)

TOTAL=$(echo "$INFO" | awk '{print $2}')
USADO=$(echo "$INFO" | awk '{print $3}')
LIBRE=$(echo "$INFO" | awk '{print $4}')
PORC=$(echo "$INFO" | awk '{print $5}')

echo "📂 Punto analizado: $TARGET"
echo "💾 Tamaño total: $TOTAL"
echo "📉 Usado: $USADO"
echo "📈 Libre: $LIBRE"
echo "🔢 Porcentaje usado: $PORC"

echo
echo "✔️ Lectura completada."
