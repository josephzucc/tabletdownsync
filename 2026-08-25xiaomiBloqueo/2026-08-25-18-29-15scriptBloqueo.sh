adb bugreport bugreport_freeze.zip
hoy=$(date +"%Y-%m-%d");ahora=$(date +"%Y-%m-%d-%H-%M-%S"); echo -e "${hoy}\t${ahora}";adb logcat -v threadtime > ${ahora}logcat_en_vivo &
adb shell ps -A -T -o PID,TID,S,CMD | grep -E "system_server|settings|netmgrd|rild"
