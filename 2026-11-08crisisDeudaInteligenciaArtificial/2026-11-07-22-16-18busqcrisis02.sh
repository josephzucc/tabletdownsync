#python3 cnmv_oir.py sonda --entidad "Merlin"
#python3 cnmv_oir.py sonda --entidad "ACS"
#python3 cnmv_oir.py sonda --entidad "Ferrovial"
#python3 cnmv_oir.py sonda --entidad "Grenergy"
#python3 cnmv_oir.py sonda --entidad "Solaria"

python3 cnmv_oir.py --version
python3 cnmv_oir.py buscar --ultimos 1
echo "Debe decir \"Leidas 4 de 4 paginas\" (o las que haya) y mostrar entidad, hora y tipo en cada registro."
python3 cnmv_oir.py sonda --entidad "Merlin"
python3 cnmv_oir.py vigilar
echo "Debe recorrer ACS, Ferrovial, Merlin, Grenergy y Solaria y mostrar los avisos de los ultimos 7 dias."
python3 cnmv_oir.py vigilar
echo "Repetido enseguida, debe decir \"0 nuevos\" en cada empresa."
python3 cnmv_oir.py buscar --entidad "Solaria" --ultimos 30
echo "Los titulos largos deben salir completos, partidos en varias lineas."
