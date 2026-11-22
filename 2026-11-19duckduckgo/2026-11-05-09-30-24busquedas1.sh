source "$functest";
echo "Busqueda La Adrada ultimos 15 dias....";
gn;sev=$(getYes 7;echo $yes);fift=$(getYes 15;echo $yes);echo ${today} $fift;python3 duck.py buscar "la adrada -sotillo" --desde ${today} --hasta ${fift}
echo "Busqueda San Martin de Valdeiglesias ultimos 15 dias....";
gn;sev=$(getYes 7;echo $yes);fift=$(getYes 15;echo $yes);echo ${today} $fift;python3 duck.py buscar "san martin de valdeiglesias" --desde ${today} --hasta ${fift}
echo "Busqueda ciberseguridad ultimos 15 dias....";
gn;sev=$(getYes 7;echo $yes);fift=$(getYes 15;echo $yes);echo ${today} $fift;python3 duck.py buscar "ciberseguridad" --desde ${today} --hasta ${fift}
