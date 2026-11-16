#python3 cnmv_oir.py sonda --entidad "Merlin"
#python3 cnmv_oir.py sonda --entidad "ACS"
#python3 cnmv_oir.py sonda --entidad "Ferrovial"
#python3 cnmv_oir.py sonda --entidad "Grenergy"
#python3 cnmv_oir.py sonda --entidad "Solaria"

#python3 cnmv_oir.py --version
#python3 cnmv_oir.py buscar --ultimos 1
#echo "Debe decir \"Leidas 4 de 4 paginas\" (o las que haya) y mostrar entidad, hora y tipo en cada registro."
#python3 cnmv_oir.py sonda --entidad "Merlin"
#python3 cnmv_oir.py vigilar
#echo "Debe recorrer ACS, Ferrovial, Merlin, Grenergy y Solaria y mostrar los avisos de los ultimos 7 dias."
#python3 cnmv_oir.py vigilar
#echo "Repetido enseguida, debe decir \"0 nuevos\" en cada empresa."
#python3 cnmv_oir.py buscar --entidad "Solaria" --ultimos 30
#echo "Los titulos largos deben salir completos, partidos en varias lineas."

#echo "buscar centros de datos, centro de datos, data center, datacenter";
#python3 fuentes_ia.py boa --dias 120 --texto "centro de datos,centros de datos,data center,datacenter"
#echo "plan de interes general, interes autonomico, campos";
#python3 fuentes_ia.py boa --dias 120 --texto "plan de interes general,interes autonomico,campus"
#echo "subestacion, linea de alta tension, 220kv, 400 kv";
#python3 fuentes_ia.py boa --dias 60 --texto "subestacion,linea de alta tension,220 kv,400 kv"

#echo "busquedas borme"
#python3 fuentes_ia.py borme --dias 14 --nombre "Merlin,Solaria,Grenergy,Ferrovial,ACS" --solo-nombres --nombre-en-empresa
#python3 fuentes_ia.py red --archivo RUTA_DEL_EXCEL --ccaa Aragon --min-mw 20

#echo "red electrica con rutaExcel correa"
#RUTA_DEL_EXCEL="2026-11-08-11-17-05redelectrica_publicacion-solicitudes-acceso-demanda-octubre-2026.xlsx";
#python3 fuentes_ia.py red --archivo ${RUTA_DEL_EXCEL} --ccaa Aragon --min-mw 20

#echo "red Electrica nuevo analisis"
#python3 ree.py analizar --ccaa "" --top 40

#echo "fuentes_ia.py fuentes boa --texto \"datacenter,400 kv\" --dias 120";
#python3 fuentes_ia.py --dir /storage/emulated/0/Download/DownloadSync/fuentes boa --texto "datacenter,400 kv" --dias 120

echo "fuentes_ia.py concursal";
python3 fuentes_ia.py concursal

echo "ree.py capacidad"
python3 ree.py capacidad

# Ejemplos para vigilar la crisis de financiacion de infraestructura de IA.
# Ejecutar por bloques (copiar/pegar o "bash crisis_ejemplos.sh"). Ajusta las rutas de tus programas.
# Requisito: python3 ree.py serve --fondo  (para la parte de Red Electrica)

# 1) Red Electrica: descargar lo nuevo y analizar (Firefox abierto con la extension REE Puente)
python3 ree.py serve --fondo
python3 ree.py despertar
sleep 75
python3 ree.py estado
python3 ree.py analizar --ccaa Aragon            # solicitudes de acceso del mes y comparacion con el mes anterior
python3 ree.py analizar --ccaa "" --top 30       # todas las comunidades
python3 ree.py capacidad --ccaa Aragon           # margen disponible por nudo (demanda)
python3 ree.py capacidad --ccaa "" --top 15      # ranking de comunidades por margen bruto

# 2) Concursos y empresas del sector (BORME, datos abiertos oficiales)
python3 fuentes_ia.py borme --dias 7 --provincia zaragoza,huesca,teruel
python3 fuentes_ia.py concursal --dias 7
python3 fuentes_ia.py borme --dias 14 --nombre "Merlin,Solaria,Grenergy,Ferrovial,ACS" --solo-nombres

# 3) Financiacion y deuda: SEC (CoreWeave) y CNMV
python3 fuentes_ia.py sec --ticker CRWV --dias 30 --deuda
python3 fuentes_ia.py sec --ticker CRWV --formularios 8-K --dias 30
python3 cnmv_oir.py sonda
python3 cnmv_oir.py vigilar                  # recorre tu vigilar.txt y muestra lo nuevo

# 4) Boletines oficiales: proyectos y autorizaciones de centros de datos (frase entre comillas)
python3 fuentes_ia.py boa --texto "centro de datos" --dias 90
python3 fuentes_ia.py boa --texto "centros de datos" --dias 90
python3 bop.py web bocyl -c '"data center"' --6meses
python3 bop.py web boe -c '"centro de datos"' --ultimos 90
python3 subv.py buscar -c "centro de datos" --12meses --detalle
