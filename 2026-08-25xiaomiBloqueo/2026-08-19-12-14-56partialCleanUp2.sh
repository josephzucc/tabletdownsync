# Script para desinstalar apps pesadas
adb shell "pm list packages -3 > /sdcard/apps.txt"
adb pull /sdcard/apps.txt

# Luego desinstalar una por una (ejemplos):
adb uninstall com.facebook.katana
adb uninstall com.instagram.android
adb uninstall com.twitter.android
adb shell "pm uninstall -k --user 0 com.mi.global.browser"
adb shell "pm uninstall -k --user 0 com.miui.daemon"

# Limpiar caché individual de apps grandes
adb shell "pm clear --user 0 com.google.android.youtube"
adb shell "pm clear --user 0 com.netflix.mediaclient"
