#!/data/data/com.termux/files/usr/bin/bash

# Ejecuta ESTE script en tiempo real mientras esperas el reinicio

OUTPUT_DIR="$HOME/live_auth_capture_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "Capturando logs en vivo... Espera el próximo PIN de desbloqueo"
echo "Presiona Ctrl+C cuando haya terminado"

# Logcat en tiempo real
adb logcat -v time > "$OUTPUT_DIR/live_logcat.txt" &
LOGCAT_PID=$!

# dmesg en tiempo real
adb shell "dmesg -w" > "$OUTPUT_DIR/live_dmesg.txt" &
DMESG_PID=$!

# Esperar 2 minutos
echo "Capturando por 120 segundos..."
sleep 120

# Detener captura
kill $LOGCAT_PID 2>/dev/null
kill $DMESG_PID 2>/dev/null

# Extraer eventos de auth
echo "Procesando eventos de autenticación..."
grep -iE "auth|unlock|pattern|password|pin|lock" "$OUTPUT_DIR/live_logcat.txt" > "$OUTPUT_DIR/auth_events.txt"

echo "Captura completada en: $OUTPUT_DIR"
echo "Busca especialmente en:"
echo "  - live_logcat.txt (eventos justo antes de reboot)"
echo "  - live_dmesg.txt (kernel messages)"
echo "  - auth_events.txt (solo auth)"
