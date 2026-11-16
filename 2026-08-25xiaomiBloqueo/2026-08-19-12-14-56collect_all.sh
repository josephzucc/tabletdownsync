# Paste this entire script:
#!/data/data/com.termux/files/usr/bin/sh
OUTPUT_DIR="$HOME/device_diagnostics_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "Collecting thermal data..."
for zone in /sys/class/thermal/thermal_zone*/temp; do
  name=$(basename $(dirname $zone))
  cat $zone 2>/dev/null >> "$OUTPUT_DIR/thermal_temps.txt"
  echo "Zone: $name Temp: $(cat $zone 2>/dev/null)m°C" >> "$OUTPUT_DIR/thermal_temps.txt"
done

echo "Collecting battery data..."
for item in capacity status health temperature voltage_now; do
  echo "$item: $(cat /sys/class/power_supply/battery/$item 2>/dev/null)" >> "$OUTPUT_DIR/battery_health.txt"
done

echo "Collecting storage..."
df -h >> "$OUTPUT_DIR/storage_space.txt"

echo "Collecting CPU info..."
cat /proc/cpuinfo >> "$OUTPUT_DIR/cpu_info.txt"

echo "Collecting logs..."
logcat -d -t 500 >> "$OUTPUT_DIR/error_logs.txt" 2>&1
dmesg | tail -n 500 >> "$OUTPUT_DIR/kernel_dmesg.txt" 2>&1

echo "Done! Files saved in: $OUTPUT_DIR"
ls -la "$OUTPUT_DIR"
