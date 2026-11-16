echo "BORME (Aragón)"
python3 fuentes_ia.py borme --dias 7 --provincia zaragoza,huesca,teruel
echo "Concursal (BORME)"
python3 fuentes_ia.py concursal --dias 3
echo "Vigilar empresas por nombre"
python3 fuentes_ia.py borme --dias 14 --nombre "Merlin,Solaria,Grenergy,Ferrovial,ACS" --solo-nombres
echo "SEC"
python3 fuentes_ia.py sec --ticker CRWV --dias 60 --deuda
echo "Red Eléctrica"
python3 fuentes_ia.py red --archivo RUTA.xlsx --ccaa Aragon --min-mw 20
echo "BOA"
python3 fuentes_ia.py boa --dias 90 --texto "centro de datos,interes autonomico,declaracion de impacto,subestacion"
