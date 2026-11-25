#python3 bop.py web boe -c '"centro de datos"' --ultimos 90
#python3 bop.py web boe -c '"centros de datos"' --ultimos 90
#python3 bop.py web boe -c '"data center"' --ultimos 90
#python3 bop.py web bocyl -c '"centro de datos"' --6meses
#python3 bop.py web bocyl -c '"centros de datos"' --6meses
#python3 bop.py web bocyl -c '"data center"' --6meses
#python3 bop.py web bocm -c '"centro de datos"' --6meses
#python3 bop.py web bocm -c '"centros de datos"' --6meses
#python3 bop.py web bocm -c '"data center"' --6meses
python3 subv.py buscar -c '"centros de datos"' --12meses --detalle --modo alguna
python3 subv.py buscar -c '"centro de datos"' --12meses --detalle --modo alguna
python3 subv.py buscar -c '"data center"' --12meses --detalle --modo alguna
