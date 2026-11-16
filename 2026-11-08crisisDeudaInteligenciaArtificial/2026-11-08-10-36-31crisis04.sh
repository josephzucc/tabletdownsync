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

echo "busquedas borme"
python3 fuentes_ia.py borme --dias 14 --nombre "Merlin,Solaria,Grenergy,Ferrovial,ACS" --solo-nombres --nombre-en-empresa
#python3 fuentes_ia.py red --archivo RUTA_DEL_EXCEL --ccaa Aragon --min-mw 20
