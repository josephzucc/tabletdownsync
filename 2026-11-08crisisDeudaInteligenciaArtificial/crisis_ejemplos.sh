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
