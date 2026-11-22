clear
echo "Busqueda VACL-036, TR-13, VACL-149 y VACL-037 en Lista Concesiones Transportes Regionales"
cat 2026-08-02-11-38-00juntaCastillaLeon_concesionesTransportesPorCarretera.txt | grep -i 'VACL-036'
cat 2026-08-02-11-38-00juntaCastillaLeon_concesionesTransportesPorCarretera.txt | grep -i 'TR-13'
cat 2026-08-02-11-38-00juntaCastillaLeon_concesionesTransportesPorCarretera.txt | grep -i 'VACL-149'
cat 2026-08-02-11-38-00juntaCastillaLeon_concesionesTransportesPorCarretera.txt | grep -i 'VACL-037'
echo -e "\n\n\n\nBusqueda de estas concesiones en Boletin Castilla-Leon"
bash 2026-08-12-17-06-35busquedaConcesionesCastillaLeon.sh VACL-036
bash 2026-08-12-17-06-35busquedaConcesionesCastillaLeon.sh TR-13
bash 2026-08-12-17-06-35busquedaConcesionesCastillaLeon.sh VACL-149
bash 2026-08-12-17-06-35busquedaConcesionesCastillaLeon.sh VACL-037
