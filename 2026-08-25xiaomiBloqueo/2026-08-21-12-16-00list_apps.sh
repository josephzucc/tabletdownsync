#!/data/data/com.termux/files/usr/bin/bash

OUTPUT_DIR="$HOME/apps_list_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

IP_XIAOMI="192.168.0.11:38571"

echo "╔══════════════════════════════════════════════╗"
echo "║  LISTADO APLICACIONES PARA BORRAR          ║"
echo "╚══════════════════════════════════════════════╝"

# Verificar ADB
adb devices | grep -q "device" || {
    adb connect $IP_XIAOMI
    sleep 3
}

echo ""
echo "=== APPS DE TERCEROS INSTALADAS ==="
adb shell "pm list packages -3 | sed 's/package://' | sort" > "$OUTPUT_DIR/third_party_apps.txt"
cat "$OUTPUT_DIR/third_party_apps.txt"

echo ""
echo "=== TOP 20 APLICACIONES POR TAMAÑO ==="
adb shell "du -sh /data/app/* 2>/dev/null | sort -hr | head -20"

echo ""
echo "=== APPS XIAOMI QUE SE PUEDEN DESACTIVAR ==="
adb shell "pm list packages | grep xiaomi" > "$OUTPUT_DIR/xiaomi_apps.txt"
cat "$OUTPUT_DIR/xiaomi_apps.txt"

echo ""
echo "=== COMANDOS PARA ELIMINAR (copiar y pegar) ==="
cat > "$OUTPUT_DIR/uninstall_commands.txt" << 'EOF'
# Copia y pega estos comandos uno por uno:

# Redes sociales pesadas:
adb uninstall com.facebook.katana
adb uninstall com.instagram.android
adb uninstall com.twitter.android
adb uninstall com.snapchat.android
adb uninstall com.tiktok.android

# Streaming:
adb uninstall com.netflix.mediaclient
adb uninstall com.spotify.music

# Juegos (ejemplos):
# adb uninstall com.ea.game.pvzfree_row
# adb uninstall com.kiloo.subwaysurf

# Apps Xiaomi que se pueden desactivar:
adb shell "pm disable-user --user 0 com.miui.daemon"
adb shell "pm disable-user --user 0 com.mi.global.browser"
adb shell "pm disable-user --user 0 com.xiaomi.scanner"
EOF

cat "$OUTPUT_DIR/uninstall_commands.txt"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  LISTADO COMPLETADO                        ║"
echo "╠══════════════════════════════════════════════╣"
ls -la "$OUTPUT_DIR"
echo "╚══════════════════════════════════════════════╝"

exit 0
