# Paste this content:
for zone in $(ls /sys/class/thermal/thermal_zone*/type 2>/dev/null); do
  name=$(basename ${zone%/*})
  temp=$(cat /sys/class/thermal/${name}/temp 2>/dev/null)
  type=$(cat /sys/class/thermal/${zone}/type 2>/dev/null)
  echo "${type}: ${temp}°C"
done
