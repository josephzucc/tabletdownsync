con=217;d=30;h=$((24-8));l=$(head -c 121 /dev/zero | tr '\0' '=');echo -ne "\n\n\n${l}gasto luz 24 horas al dia menos 8 horas que no usamos luz, son 16 horas al dia\n30 dias por factura son 480 horas por factura\nal precio de la energia se calcula un 5,12% de Impuesto de Electricidad\nY a eso un 21% de IVA\nAdemas existen Alquiler de contador 0,03 euros por dia y Termino fijo 10 euros en octubre 2026\ncalculamos 480 horas por factura por un consumo de 217 kwh dan un valor de\ncuantos kwH por cada hora\t\t";n=$(echo "scale=4;(($d*$h)/$con)" | bc);printf "%8.4f\n" $n;
echo -ne "Precio alto 0,40 centimos:\t\t";n=$(echo "scale=4;(((($d*$h)/$con)*.40)*1.0512)*1.21" | bc);printf "%8.4f\n" $n;
echo -ne "Precio alto 0,40 centimos en 30 dias:\t";n=$(echo "scale=4;((217*.40)*1.0512)*1.21" | bc);printf "%8.4f\n" $n
echo -ne "Precio medio 0,23 centimos:\t\t";n=$(echo "scale=4;(((($d*$h)/$con)*.23)*1.0512)*1.21" | bc);printf "%8.4f\n" $n;
echo -ne "Precio medio 0,23 centimos en 30 dias:\t";n=$(echo "scale=4;((217*.23)*1.0512)*1.21" | bc);printf "%8.4f\n" $n;
echo -ne "Precio bajo 0,05 centimos:\t\t";n=$(echo "scale=4;(((($d*$h)/$con)*.05)*1.0512)*1.21" | bc);printf "%8.4f\n" $n;
echo -ne "Precio bajo 0,05 centimos en 30 dias:\t";n=$(echo "scale=4;((217*.05)*1.0512)*1.21" | bc);printf "%8.4f\n" $n;
