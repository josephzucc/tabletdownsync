# Script para listar apps de sistema que se pueden desactivar
adb shell "pm list packages -s | grep -v 'android\|com.google\|com.android.systemui'"

# Ejemplos de apps que se pueden desactivar (NO desinstalar):
adb shell "pm disable-user --user 0 com.miui.daemon"           # Xiaomi Analytics
adb shell "pm disable-user --user 0 com.mi.global.browser"    # Navegador Mi
adb shell "pm disable-user --user 0 com.xiaomi.scanner"       # Scanner
adb shell "pm disable-user --user 0 com.miui.virtualsim"      # Roaming virtual
adb shell "pm disable-user --user 0 com.miui.hybrid"          # Quick apps
