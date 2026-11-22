echo "=== IDENTIFICACION ==="
uname -a
echo

echo "=== ANDROID ==="
getprop ro.build.version.release
getprop ro.build.version.sdk
getprop ro.build.version.security_patch
getprop ro.product.model
getprop ro.product.name
getprop ro.product.device
getprop ro.build.version.incremental
echo

echo "=== HARMONY ==="
getprop ro.build.version.huawei
getprop ro.build.version.emui
getprop ro.build.version.harmony
echo

echo "=== USUARIO TERMUX ==="
whoami
id
echo

echo "=== ADB ==="
command -v adb || echo "adb NO instalado"
adb version 2>&1
echo

echo "=== ADB SOCKETS ==="
ls -l /dev/socket/ | grep -Ei 'adb|adbd' 2>/dev/null
echo

echo "=== PROCESOS ADB ==="
ps -A 2>/dev/null | grep -Ei 'adbd|adb' || echo "No aparece adbd"
echo

echo "=== PUERTOS ADB ==="
ss -lnt 2>/dev/null | grep -Ei '5555|5037' || echo "No aparecen puertos 5555/5037"
echo

echo "=== PERMISOS IMPORTANTES ==="
ls -l /system/bin/adb /system/bin/adbd /system/bin/run-as 2>&1
echo

echo "=== SHIZUKU ==="
pm list packages 2>/dev/null | grep -i shizuku || echo "Shizuku NO instalado"
echo

echo "=== TERMUX ==="
echo "TERMUX_IS_DEBUGGABLE_BUILD=$TERMUX_IS_DEBUGGABLE_BUILD"
