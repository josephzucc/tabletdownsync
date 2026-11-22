#!/data/data/com.termux/files/usr/bin/bash

# Colores
RED="\033[1;31m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
BLUE="\033[1;34m"
RESET="\033[0m"

# Función para limpiar pantalla
cls() {
    clear
}

# Panel 1: Procesos
panel_procesos() {
    cls
    echo -e "${BLUE}=== Panel de Procesos (top 20 por CPU) ===${RESET}"
    top -b -n 1 | head -n 20
    echo ""
    echo -e "${YELLOW}Presiona ENTER para volver al menú...${RESET}"
    read
}

# Panel 2: Actividad de red (Huawei-compatible)
panel_red() {
    cls
    echo -e "${BLUE}=== Panel de Red (Huawei-compatible) ===${RESET}"
    echo -e "${GREEN}Ping a servidores conocidos:${RESET}"
    for h in 1.1.1.1 8.8.8.8 google.com; do
        echo -e "${YELLOW}Ping a $h${RESET}"
        ping -c 1 "$h" 2>/dev/null | head -n 3
        echo ""
    done
    echo -e "${YELLOW}Presiona ENTER para volver al menú...${RESET}"
    read
}

# Panel 3: DNS
panel_dns() {
    cls
    echo -e "${BLUE}=== Panel DNS ===${RESET}"
    for d in google.com microsoft.com facebook.com cloudflare.com; do
        echo -e "${GREEN}Resolviendo $d${RESET}"
        getent hosts "$d"
        echo ""
    done
    echo -e "${YELLOW}Presiona ENTER para volver al menú...${RESET}"
    read
}

# Panel 4: APKs
panel_apks() {
    cls
    echo -e "${BLUE}=== Panel de APKs ===${RESET}"
    echo -e "${GREEN}APKs encontrados:${RESET}"
    find /sdcard -type f -name "*.apk" 2>/dev/null
    echo ""
    echo -e "${GREEN}Top 20 archivos más grandes:${RESET}"
    find /sdcard -type f -printf "%s %p\n" 2>/dev/null | sort -nr | head -n 20
    echo ""
    echo -e "${YELLOW}Presiona ENTER para volver al menú...${RESET}"
    read
}

# Panel 5: Logs
panel_logs() {
    cls
    echo -e "${BLUE}=== Panel de Logs (errores recientes) ===${RESET}"
    logcat -d 2>/dev/null | grep -i "error" | head -n 50
    echo ""
    echo -e "${YELLOW}Presiona ENTER para volver al menú...${RESET}"
    read
}

# Panel 6: Reglas de sospecha
panel_sospecha() {
    cls
    echo -e "${BLUE}=== Reglas de Sospecha ===${RESET}"

    RIESGO=0

    # Regla 1: APKs gigantes
    if find /sdcard -type f -name "*.apk" -size +200M | grep -q .; then
        echo -e "${RED}- APKs mayores de 200MB detectados${RESET}"
        RIESGO=$((RIESGO+2))
    else
        echo -e "${GREEN}- No hay APKs gigantes${RESET}"
    fi

    # Regla 2: Permisos peligrosos detectables
    if find /sdcard -type f -name "*.apk" -exec aapt dump permissions {} 2>/dev/null \; | grep -i "READ_SMS" >/dev/null; then
        echo -e "${RED}- Permiso READ_SMS detectado en algún APK${RESET}"
        RIESGO=$((RIESGO+3))
    else
        echo -e "${GREEN}- No se detectaron permisos peligrosos${RESET}"
    fi

    # Regla 3: Errores masivos en logcat
    ERRS=$(logcat -d | grep -i "error" | wc -l)
    if [ "$ERRS" -gt 200 ]; then
        echo -e "${RED}- Más de 200 errores en logcat${RESET}"
        RIESGO=$((RIESGO+1))
    else
        echo -e "${GREEN}- Logcat sin exceso de errores${RESET}"
    fi

    echo ""
    echo -e "${BLUE}=== Nivel de Riesgo ===${RESET}"

    if [ "$RIESGO" -ge 6 ]; then
        echo -e "${RED}RIESGO ALTO${RESET}"
    elif [ "$RIESGO" -ge 3 ]; then
        echo -e "${YELLOW}RIESGO MEDIO${RESET}"
    else
        echo -e "${GREEN}RIESGO BAJO${RESET}"
    fi

    echo ""
    echo -e "${YELLOW}Presiona ENTER para volver al menú...${RESET}"
    read
}

# Menú principal
menu() {
    while true; do
        cls
        echo -e "${BLUE}=== DASHBOARD DE SEGURIDAD TERMUX ===${RESET}"
        echo ""
        echo -e "${GREEN}1) Procesos${RESET}"
        echo -e "${GREEN}2) Actividad de red${RESET}"
        echo -e "${GREEN}3) DNS${RESET}"
        echo -e "${GREEN}4) APKs${RESET}"
        echo -e "${GREEN}5) Logs${RESET}"
        echo -e "${GREEN}6) Reglas de sospecha${RESET}"
        echo -e "${RED}0) Salir${RESET}"
        echo ""
        echo -ne "${YELLOW}Selecciona una opción: ${RESET}"
        read opt

        case $opt in
            1) panel_procesos ;;
            2) panel_red ;;
            3) panel_dns ;;
            4) panel_apks ;;
            5) panel_logs ;;
            6) panel_sospecha ;;
            0) exit ;;
            *) echo "Opción inválida"; sleep 1 ;;
        esac
    done
}

menu
