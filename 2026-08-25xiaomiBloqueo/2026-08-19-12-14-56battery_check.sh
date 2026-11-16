# Paste this:
echo "=== BATTERY INFO ===" 
for item in capacity status health temperature voltage_now cycle_count; do
  val=$(cat /sys/class/power_supply/battery/$item 2>/dev/null)
  echo "$item: $val"
done

# Also check kernel battery info
echo ""
echo "=== PROC ACPI/POWER ===" 
cat /proc/acpi/battery/BAT0/info 2>/dev/null || echo "No ACPI battery info"
cat /proc/acpi/battery/BAT0/state 2>/dev/null || echo "No ACPI battery state"
