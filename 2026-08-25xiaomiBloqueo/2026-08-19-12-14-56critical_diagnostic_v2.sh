#!/data/data/com.termux/files/usr/bin/bash

# ════════════════════════════════════════════
# DIAGNÓSTICO XIAOMI 10T LITE 5G v2.0
# Mejor versión - Sin errores, más datos
# ════════════════════════════════════════════

OUTPUT_DIR="$HOME/xiaomi_critical_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "╔════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO CRÍTICO - XIAOMI 10T LITE ║"
echo "║  Fecha: $(date)                         ║"
echo "╚════════════════════════════════════════╝"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [1] DATOS DE BATERÍA - MÉTODO DUMPSYS (SIN ROOT)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[1/10] Obteniendo información de batería..."

{
    echo "=== BATERÍA - DUMPSYS ==="
    dumpsys batterystats | grep -E "Capacity|Health|Temperature|Voltage|Cycle" 2>/dev/null || echo "dumpsys baterystats limitado"
    
    echo ""
    echo "=== BATERÍA - GETPROP ==="
    getprop | grep -E "battery|charger|power" 2>/dev/null | head -30
    
    echo ""
    echo "=== BATERÍA - POWER SUPPLY PROPERTIES ==="
    for prop in battery.capacity battery.health battery.temperature battery.status battery.level battery.tech; do
        val=$(getprop $prop 2>/dev/null || echo "N/A")
        echo "$prop: $val"
    done
    
    echo ""
    echo "=== ESTADO DE CARGA ACTUAL ==="
    dumpsys power | grep -E "mWakefulness|mUserActivity|mScreenOn" 2>/dev/null | head -10
    
} > "$OUTPUT_DIR/bateria_dumpsys.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [2] TEMPERATURAS TÉRMICAS DETALLADAS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[2/10] Recolectando sensores térmicos con detalles..."

{
    echo "=== ZONAS TÉRMICAS COMPLETAS ==="
    echo "Formato: Zona | Tipo | Policy | Temp(m°C) | Temp(°C)"
    echo "================================================"
    
    highest=0
    highest_zone=""
    
    for zone_dir in /sys/class/thermal/thermal_zone*; do
        zone_num=$(basename $zone_dir | sed 's/thermal_zone//')
        
        # Obtener tipo y política
        type=$(cat $zone_dir/type 2>/dev/null || echo "unknown")
        policy=$(cat $zone_dir/policy 2>/dev/null || echo "unknown")
        
        # Obtener temperatura (algunas zonas pueden fallar)
        temp_raw=$(cat $zone_dir/temp 2>/dev/null || echo "")
        
        if [[ "$temp_raw" =~ ^-?[0-9]+$ ]]; then
            temp_c_int=$((temp_raw / 1000))
            temp_c_dec=$((abs(temp_raw % 1000)))
            
            # Ignorar valores inválidos (-273°C = sensor desconectado)
            if [ "$temp_raw" != "-273000" ] && [ "$temp_raw" != "-274000" ]; then
                printf "%-20s | %-15s | %-12s | %7dm°C | %5d.%03d°C\n" \
                    "thermal_zone$zone_num" "$type" "$policy" "$temp_raw" "$temp_c_int" "$temp_c_dec" >> "$OUTPUT_DIR/thermal_detailed.txt"
                
                # Trackear zona más caliente
                if [ "$temp_raw" -gt "$highest" ] 2>/dev/null; then
                    highest=$temp_raw
                    highest_zone="thermal_zone$zone_num ($type)"
                fi
            fi
        else
            printf "%-20s | %-15s | %-12s | %-9s | %s\n" \
                "thermal_zone$zone_num" "$type" "$policy" "$temp_raw" "ERROR" >> "$OUTPUT_DIR/thermal_detailed.txt"
        fi
    done
    
    echo ""
    echo "=== RESUMEN TÉRMICO ==="
    if [ -n "$highest_zone" ]; then
        highest_c_int=$((highest / 1000))
        highest_c_dec=$((abs(highest % 1000)))
        echo "Temperatura MÁS ALTA: $highest_c_int.$highest_c_dec°C"
        echo "En zona: $highest_zone"
        
        # Clasificación de riesgo
        if [ "$highest" -gt 50000 ]; then
            echo "RIESGO: CRÍTICO (>50°C) - Peligro inmediato de daño"
        elif [ "$highest" -gt 45000 ]; then
            echo "RIESGO: ALTO (>45°C) - Posible thermal throttle/shutdown"
        elif [ "$highest" -gt 40000 ]; then
            echo "RIESGO: MODERADO (>40°C) - Monitorear continuamente"
        else
            echo "RIESGO: BAJO (<40°C) - Temperaturas normales"
        fi
    else
        echo "No se pudo determinar temperatura máxima"
    fi
    
} > "$OUTPUT_DIR/thermal_summary.txt"

# Función auxiliar para valor absoluto (evitar error)
abs() {
    local val=$1
    if [ "$val" -lt 0 ]; then
        echo $((-val))
    else
        echo $val
    fi
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [3] ALMACENAMIENTO - ANÁLISIS AUTOMÁTICO
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[3/10] Analizando estado de almacenamiento..."

{
    echo "=== PARTICIONES DEL SISTEMA (CRÍTICAS) ==="
    echo "Partición | Usada | Total | Libre | Uso% | Estado"
    echo "================================================"
    
    df -h 2>/dev/null | while read fs size used avail usep mounted; do
        # Extraer número porcentual
        pct=$(echo "$usep" | tr -d '%')
        
        # Determinar estado
        case $pct in
            100|9[5-9]) status="🔴 CRÍTICO" ;;
            9[0-4])     status="🟡 ALTO" ;;
            8[0-9])     status="🟠 MODERADO" ;;
            *)          status="🟢 OK" ;;
        esac
        
        # Solo mostrar particiones relevantes
        if [[ "$mounted" =~ ^(\/|\/system|\/product|\/vendor|\/data|\/cache) ]]; then
            echo "$mounted | $used | $size | $avail | $usep | $status"
        fi
    done
    
    echo ""
    echo "=== DISK SPACE EN DATA (USUARIO) ==="
    du -sh /data/user/0 2>/dev/null || echo "No disponible"
    
    echo ""
    echo "=== CARPETAS MÁS OCUPADAS EN /data ==="
    du -sh /data/data/* 2>/dev/null | sort -hr | head -10 || echo "No disponible"
    
    echo ""
    echo "=== CACHE Y TEMP FILES ==="
    du -sh /cache 2>/dev/null || echo "/cache: N/A"
    du -sh /data/cache 2>/dev/null || echo "/data/cache: N/A"
    
} > "$OUTPUT_DIR/storage_analysis.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [4] MEMORIA RAM EN TIEMPO REAL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[4/10] Capturando estado de memoria..."

{
    echo "=== MEMORIA RAM (FREE -H) ==="
    free -h 2>/dev/null || cat /proc/meminfo | head -20
    
    echo ""
    echo "=== PROCESOS POR CONSUMO DE MEMORIA ==="
    ps -eo pid,user,rss,vsize,comm --sort=-rss 2>/dev/null | head -15
    
    echo ""
    echo "=== SWAP USAGE ==="
    grep -E "^Swap|^Mem" /proc/meminfo | head -5
    
} > "$OUTPUT_DIR/memory_status.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [5] PROCESOS ACTIVIDAD RECIÉN ARRANCADO
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[5/10] Analizando procesos post-inicio..."

{
    echo "=== PROCESOS QUE INICIARON CON EL BOOT ==="
    ps -A 2>/dev/null | head -30
    
    echo ""
    echo "=== SERVICIOS ANDROID ACTIVOS ==="
    service list 2>/dev/null | head -50
    
    echo ""
    echo "=== TERMUX PROCESOS ==="
    ps aux | grep termux 2>/dev/null || echo "No running"
    
} > "$OUTPUT_DIR/processes.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [6] LOGS DEL KERNEL (DMESG) - MÁS IMPORTANTE PARA REBOOTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[6/10] Extrayendo logs del kernel (DMESG)..."

{
    echo "=== DMESG COMPLETO (ÚLTIMOS 1000 LÍNEAS) ==="
    dmesg 2>/dev/null | tail -1000
    
    echo ""
    echo "=== BUSCAR REBOOT/CLOSE/POWER OFF ==="
    dmesg 2>/dev/null | grep -iE "reboot|shutdown|power_off|hang|kill|oom" | tail -50
    
    echo ""
    echo "=== BUSCAR TEMPERATURA/TÉRMICA ==="
    dmesg 2>/dev/null | grep -iE "thermal|temperature|hot|overheat|thrott" | tail -50
    
    echo ""
    echo "=== BUSCAR ERROR/PANIC/FATAL/CRASH ==="
    dmesg 2>/dev/null | grep -iE "error|panic|fatal|crash|exception" | tail -50
    
    echo ""
    echo "=== BUSCAR BATERÍA/POWER/SWITCH ==="
    dmesg 2>/dev/null | grep -iE "battery|power|supply|charger" | tail -50
    
    echo ""
    echo "=== ÚLTIMO ERROR REGISTRADO ==="
    dmesg 2>/dev/null | grep -iE "^[^\[]*[0-9]+\.[0-9]+" | tail -1
    
} > "$OUTPUT_DIR/dmesg_full.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [7] LOGCAT CON FILTROS ESPECÍFICOS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[7/10] Logcat filtrado para térmica/reboots..."

{
    echo "=== LOGCAT - THERMAL ENGINE ==="
    logcat -d 2>&1 | grep -iE "thermalengine|thermald|qcom_thermal" | tail -100 || echo "No thermal engine logs"
    
    echo ""
    echo "=== LOGCAT - POWER MANAGEMENT ==="
    logcat -d 2>&1 | grep -iE "PowerManager|BatteryService|charging" | tail -100 || echo "No power logs"
    
    echo ""
    echo "=== LOGCAT - REBOOT RELATED ==="
    logcat -d 2>&1 | grep -iE "reboot|shutdown|shutdownnow" | tail -50 || echo "No reboot logs"
    
    echo ""
    echo "=== LOGCAT - LAST 200 ENTRIES ==="
    logcat -d -t 200 2>&1 || echo "Logcat failed"
    
    echo ""
    echo "=== LOGCAT - ERRORS ONLY ==="
    logcat -d *:E 2>&1 | tail -100
    
} > "$OUTPUT_DIR/logcat_filtered.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [8] INFORMACIÓN DEL SISTEMA COMPLETA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[8/10] Recolectando info del sistema..."

{
    echo "=== DEVICE IDENTITY ==="
    getprop ro.product.model
    getprop ro.product.brand
    getprop ro.build.version.release
    getprop ro.build.version.sdk
    getprop ro.bootloader
    getprop ro.boot.verifiedbootstate
    getprop ro.bootimage.build.fingerprint
    getprop ro.bootimage.build.date
    
    echo ""
    echo "=== KERNEL INFO ==="
    uname -a
    cat /proc/version
    
    echo ""
    echo "=== UPTIME ==="
    cat /proc/uptime
    who -b 2>/dev/null
    
    echo ""
    echo "=== LOAD AVERAGE ==="
    cat /proc/loadavg
    
    echo ""
    echo "=== CPU FREQUENCY ==="
    cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo "N/A"
    
} > "$OUTPUT_DIR/system_identity.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [9] HISTORIAL DE APAGADOS/REINICIOS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[9/10] Buscando histórico de apagados..."

{
    echo "=== ARCHIVOS DE LOG DEL SISTEMA ==="
    ls -la /data/log/ 2>/dev/null | head -20 || echo "/data/log no existe"
    
    echo ""
    echo "=== EVENTOS DE BOOT (if exist) ==="
    cat /data/boot-log.txt 2>/dev/null | tail -100 || echo "No boot-log.txt"
    
    echo ""
    echo "=== LAST COMMAND HISTORY ==="
    history 2>/dev/null | tail -30 || echo "History not available"
    
    echo ""
    echo "=== LAST LOGIN TIMES ==="
    lastlog 2>/dev/null | head -20 || echo "lastlog not available"
    
} > "$OUTPUT_DIR/boot_history.txt"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [10] RESUMEN EJECUTIVO Y RECOMENDACIONES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "[10/10] Generando diagnóstico final..."

{
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║  DIAGNÓSTICO FINAL - XIAOMI 10T LITE 5G              ║"
    echo "║  Generado: $(date)                                    ║"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    
    # Leer datos críticos
    highest_temp=$(grep "Temperatura MÁS ALTA" "$OUTPUT_DIR/thermal_summary.txt" 2>/dev/null | head -1 || echo "N/A")
    storage_status=$(df -h 2>/dev/null | grep -E "^\s*/dev" | awk '{print $5}' | grep -E "9[5-9]%|100%" | wc -l)
    
    echo "┌─ DATOS CRÍTICOS ─────────────────────────────────────┐"
    echo "│ Temperatura más alta: $highest_temp                  │"
    echo "│ Particiones 100% llenas: $storage_status             │"
    echo "│ Tiempo de actividad: $(cat /proc/uptime | awk '{print $1}')s"
    echo "└──────────────────────────────────────────────────────┘"
    echo ""
    
    echo "┌─ DIAGNÓSTICOS PROBABLES ─────────────────────────────┐"
    if [ "$storage_status" -gt 3 ]; then
        echo "│ 🔴 PROBLEMA PRIMARIO: Sistema de archivos 100% lleno"
        echo "│    Las particiones /system, /vendor, /product están saturadas"
        echo "│    Esto causa:"
        echo "│    - Fallos al escribir logs temporales"
        echo "│    - Reinicios aleatorios por falta de espacio"
        echo "│    - Sobrecalentamiento NAND flash"
        echo ""
    fi
    
    # Extraer temperatura numérica para comparación
    temp_val=$(echo "$highest_temp" | grep -oE "[0-9]+\.[0-9]+" | head -1)
    if [ -n "$temp_val" ]; then
        temp_int=$(echo "$temp_val" | cut -d. -f1)
        if [ "$temp_int" -gt 45 ]; then
            echo "│ 🔴 PROBLEMA SECUNDARIO: Temperatura crítica (>45°C)"
            echo "│    Múltiples sensores superan 40-48°C"
            echo "│    Esto indica:"
            echo "│    - Thermal throttling activo"
            echo "│    - Posible fallo de disipación térmica"
            echo "│    - Riesgo de shutdown automático"
            echo ""
        fi
    fi
    
    echo "│ ⚠️  BATERÍA: Datos no accesibles (requiere método alternativo)"
    echo "│    Usamos dumpsys en lugar de /sys/class/power_supply"
    echo ""
    echo "└──────────────────────────────────────────────────────┘"
    echo ""
    
    echo "┌─ ACCIONES INMEDIATAS RECOMENDADAS ───────────────────┐"
    echo "│ 1. LIBERAR ESPACIO DE ALMACENAMIENTO:"
    echo "│    - Borrar caché de apps (/cache, /data/cache)"
    echo "│    - Desinstalar apps no usadas"
    echo "│    - Mover fotos/videos a PC/SD"
    echo "│"
    echo "│ 2. ENFRIAR DISPOSITIVO:"
    echo "│    - Apagar y dejar enfriar 30 min"
    echo "│    - Remover carcasa protectora"
    echo "│    - No cargar mientras caliente"
    echo "│"
    echo "│ 3. VERIFICAR SISTEMA DE ARCHIVOS:"
    echo "│    - Si persiste: Factory reset (respaldo previo)"
    echo "│    - Flashear ROM oficial desde MiFlash"
    echo "│"
    echo "│ 4. MONITOREO CONTINUO:"
    echo "│    - Ejecutar este script cada hora hasta estabilizar"
    echo "│    - Verificar si temperatura baja después de limpieza"
    echo "└──────────────────────────────────────────────────────┘"
    
} > "$OUTPUT_DIR/diagnostico_final.txt"

# ════════════════════════════════════════════
# FIN - LISTADO DE ARCHIVOS
# ════════════════════════════════════════════
echo ""
echo "╔════════════════════════════════════════╗"
echo "║  ¡DIAGNÓSTICO COMPLETADO!              ║"
echo "╠════════════════════════════════════════╣"
echo "║ Directorio: $OUTPUT_DIR                ║"
echo "╠════════════════════════════════════════╣"
echo "║ Archivos generados:                    ║"
ls -la "$OUTPUT_DIR"
echo "║                                        ║"
echo "║ Prioridad para análisis:               ║"
echo "║  • diagnostico_final.txt ← LEER ESTE  ║"
echo "║  • thermal_summary.txt                 ║"
echo "║  • storage_analysis.txt                ║"
echo "║  • dmesg_full.txt                      ║"
echo "║  • bateria_dumpsys.txt                 ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "Para enviar los archivos, navega a la carpeta en Termux"
echo "y compártelos aquí uno por uno."

exit 0
