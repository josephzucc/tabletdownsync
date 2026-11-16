#====
source $functest >/dev/null;
source $mvbbva >/dev/null;
gn;
i=0;
dir=$(pwd);dir=$(basename "$dir");
while ! [[ -f "$o" ]];do hoy=$(date -d +${i}days +"%d-%m-%Y");o="${hoy}.xls*";echo "Buscamos archivo movimientos BBVA ${o} en este directorio ${dir}....";o=$(find . -maxdepth 1 -type f -iname "*${o}*" 2>/dev/null | sort | tail -n 1 );i=$((i+1));done;
# suponemos extension de 4 caracteres xlsx, si es menos como .xls modificar
o=${o:2};
q=${#o};q=$(( q - 5 ));
p="${o:0: ${q} }";
d=$( echo $o | sed "s/${p}/${now}movbancoBBVA/g" );
echo "Cambiando nombre de ${o} a ${d}...";
mv "$o" "$d";
echo -e "Supongo xls2csv.py esta en ${dir}.\nConvierto ${d} en archivo csv...";
python3 -u xls2csv.py "${d}";
echo -e "\nActualizo variable mbbva";
avvar;
# Encabezado ,Fecha valor,Fecha,Concepto,Movimiento,Importe,Divisa,Disponible,Divisa,Observaciones
# Cambio la 2da Divisa por Divisa2
echo -e "\nCambiando encabezado Divisa,Observaciones por Divisa2 esta duplicado..."
gn;d="${now}aux.txt";sed -e "s/Divisa,Ob/Divisa2,Ob/g" $mbbva >${d};
mv $d $mbbva;
