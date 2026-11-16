#!/data/data/com.termux/files/usr/bin/bash

OUTPUT_DIR="$HOME/battery_simple_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "=== MÉTODO 1: DUMPSYS ===" > "$OUTPUT_DIR/battery_methods.txt"
adb shell "dumpsys battery" > "$OUTPUT_DIR/method1.txt" 2>> "$OUTPUT_DIR/battery_errors.txt"

echo "" >> "$OUTPUT_DIR/battery_methods.txt"
echo "=== MÉTODO 2: GETPROP ===" >> "$OUTPUT_DIR/battery_methods.txt"
adb shell "getprop | grep battery" > "$OUTPUT_DIR/method2.txt" 2>> "$OUTPUT_DIR/battery_errors.txt"

echo "" >> "$OUTPUT_DIR/battery_methods.txt"
echo "=== MÉTODO 3: SYSFS (puede fallar sin root) ===" >> "$OUTPUT_DIR/battery_methods.txt"
adb shell "cat /sys/class/power_supply/battery/capacity" > "$OUTPUT_DIR/method3.txt" 2>> "$OUTPUT_DIR/battery_errors.txt"

echo "" >> "$OUTPUT_DIR/battery_methods.txt"
echo "=== MÉTODO 4: PROP DE CARGA ===" >> "$OUTPUT_DIR/battery_methods.txt"
adb shell "getprop | grep charger" > "$OUTPUT_DIR/method4.txt" 2>> "$OUTPUT_DIR/battery_errors.txt"

cat "$OUTPUT_DIR/battery_methods.txt"

echo ""
echo "Archivos generados:"
ls -la "$OUTPUT_DIR"

if [ -s "$OUTPUT_DIR/battery_errors.txt" ]; then
    echo ""
    echo "⚠️  Se detectaron errores:"
    cat "$OUTPUT_DIR/battery_errors.txt"
fi

exit 0
