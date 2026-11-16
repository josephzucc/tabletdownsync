source ../functest.sh
gd >/dev/null
t=${1}
printf '%s: Wait %s seconds\n' "$now" "$t"
sleep $t
wait
gd >/dev/null
printf '%s: return from %s seconds\n' "$now" "$t"
