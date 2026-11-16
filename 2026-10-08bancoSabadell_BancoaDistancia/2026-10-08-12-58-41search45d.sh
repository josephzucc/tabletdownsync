source ../functest.sh
i=2;
max=45;
while (( i < max ));do
getYes ${i}
echo $yes
python3 ../correo.py -acc gmx -folders inbox -buscarfecha ${yes} -buscar sabadell -campos "de,para" -descargar -adjuntos -ver-txt -aqui -ds
i=$((i+1));
done
