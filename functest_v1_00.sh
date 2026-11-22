getNow () { now=$(date +"%Y-%m-%d-%H-%M-%S"); }
getToday () { today=$(date +"%Y-%m-%d"); }
gd () { 
  getNow;
  getToday;
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Variable \$now establecida en ${now}\nVariable \$today establecida en ${today}\nVariable \$base establecida en ${base}";
  correopy=$(find $base -type f -iname "correo.py" 2>/dev/null | head -n 1 );
  echo -e "Variable \$correopy establecida en ${correopy}";
  actfchacamb2; 
};

mailg () { local qty=10;base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";correopy=$(find $base -type f -iname "correo.py" 2>/dev/null | head -n 1);if (( $# > 0 ));then qty=${1};fi;clear;python3 ${correopy} -acc gmx -last $qty -ver-lista; }
mailgy () { local qty=10;base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";correopy=$(find $base -type f -iname "correo.py" 2>/dev/null | head -n 1);if (( $# > 0 ));then qty=${1};fi;clear;python3 ${correopy} -acc gmx yahoo -last $qty -ver-lista; }
mailall () { local qty=10;base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";correopy=$(find $base -type f -iname "correo.py" 2>/dev/null | head -n 1);if (( $# > 0 ));then qty=${1};fi;clear;python3 ${correopy} -acc todas -last $qty -ver-lista; }

actfchacamb2 () { getNow;getToday;ayer=0;echo -e "Actualizacion fecha logcambios";actual=$(pwd);echo "Folder actual ${actual}";base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";busq="logcambios";findfile="";while [[ "$findfile" == "" ]];do fecha=$(date -d -${ayer}days +"%Y-%m-%d");echo "Buscando ${busq} en ${fecha}";findfile=$(find $base -type f -iname "${fecha}*${busq}*" 2>/dev/null | sort | tail -n 1 );if [[ "$findfile" == "" ]];then echo "No encontrado ${busq} en ${fecha}....";else folder=$(dirname ${findfile});findfile=$(basename ${findfile});echo -e "Encontrado ${busq} en ${fecha}\nCambio Fecha de ${findfile} a ${hoy}${findfile:10}";cd ${folder};mv ${findfile} ${today}${findfile:10};cd ${actual};break;fi;ayer=$(( ayer + 1 ));done; }

actfchacamb () { getNow;getToday;clear;ayer=0;echo -e "Actualizacion fecha logcambios";actual=$(pwd);echo "Folder actual ${actual}";base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";busq="logcambios";findfile="";while [[ "$findfile" == "" ]];do fecha=$(date -d -${ayer}days +"%Y-%m-%d");echo "Buscando ${busq} en ${fecha}";findfile=$(find $base -type f -iname "${fecha}*${busq}*" 2>/dev/null | sort | tail -n 1 );if [[ "$findfile" == "" ]];then echo "No encontrado ${busq} en ${fecha}....";else folder=$(dirname ${findfile});findfile=$(basename ${findfile});echo -e "Encontrado ${busq} en ${fecha}\mCambio Fecha de ${findfile} a ${today}${findfile:10}";cd ${folder};mv ${findfile} ${today}${findfile:10};cd ${actual};break;fi;ayer=$(( ayer + 1 ));done; }

cambArchHoy () { gd;clear;echo -e "Cambiar Archivo hoy - (R) Jose Zuccoli";read -p "Termino ? " busq;logcambios=$(find ${base} -maxdepth 1 -type f -iname "*logcambios*" 2>/dev/null | sort | tail -n 1 );echo "Logcambios: ${logcambios}";lista1="${now}lista1.txt";i=1;while read -r file;do i=$(printf "%02d" ${i});echo "${i}. ${file:2}" >>${lista1};i=$(( i + 1 ));done < <(find . -maxdepth 1 -type f -iname "*${busq}*" 2>/dev/null | sort -r );cat ${lista1};read -p "Numero ? " nro;nro=$( printf "%02d" ${nro} );file=$(cat ${lista1} | grep "^${nro}.");file=${file:4};file2="${today}${file:10}";echo "${now}:mv $file $file2" >>${logcambios};cat ${logcambios} | tail -n 1;mv -- $file $file2;rm $lista1; }

cambCarpHoy () {
  d=1;
  today=$(date +"%Y-%m-%d");now=$(date +"%Y-%m-%d-%H-%M-%S");
  echo "$today $now";
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Cambiar Carpeta hoy - (R) Jose Zuccoli";
  read -p "Termino ? " busq;
  logcambios=$(find ${base} -maxdepth 1 -type f -iname "*logcambios*" 2>/dev/null | sort | tail -n 1 );
  echo "Logcambios: ${logcambios}";
  lista1="${now}lista1.txt"; rm -- "$lista1" 2>/dev/null;
  i=1;
  echo "Busqueda ${busq}";
  while read -r file;do i=$(printf "%02d" ${i});echo "${i}. ${file:2}" >>${lista1};
    i=$(( i + 1 ));done < <(find . -maxdepth 1 -type d -iname "*${busq}*" 2>/dev/null | sort -r );
  cat ${lista1};
  read -p "Numero ? " nro;
  nro=$( printf "%02d" ${nro} );
  file=$(cat ${lista1} | grep "^${nro}.");
  file=${file:4};
  file2="${today}${file:10}";
  echo "${now}:mv $file $file2" >>${logcambios};
  cat ${logcambios} | tail -n 1;
  mv -- $file $file2;
  rm $lista1;
}

cambCarpActual () {
  getNow;getToday;
  echo "$now $today";
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Carpeta Actual Hoy - (R) Jose Zuccoli";
  logcambios=$(find ${base} -maxdepth 1 -type f -iname "*logcambios*" 2>/dev/null | sort | tail -n 1 );
  echo "Logcambios: ${logcambios}";
  rm -- "*lista1.txt" 2>/dev/null;lista="${now}lista1.txt";echo "${lista}";i=1;
  cant=$(find . -maxdepth 1 -type d -iname "${today}*" 2>/dev/null);
  if [[ "$cant" != "" ]];then echo "\$cant no es vacio";while read -r file;do j=$(printf "%02d" #10${i});echo "${j}. ${file:2}" >>${lista};i=$(( i + 1 ));done < <(find . -maxdepth 1 -type d -iname "${today}*" 2>/dev/null | sort -r );cat "${lista}";read -p "Numero ? " nro;nro2=$( printf "%02d" ${nro} );file=$(cat ${lista1} | grep "^${nro2}.");file=${file:4};echo "${now}:cd $file" >>${logcambios};cat ${logcambios} | tail -n 1;rm -- "${lista}";cd -- "$file"; else echo "No existen carpetas Actuales";fi;
}

confebiblio () { nano /data/data/com.termux/files/home/.config/ebib/cuentas.json; }

cambIndTareas () { hoy=$(date +"%Y-%m-%d");
file=$(find . -type f -iname "${hoy}tareasgenera*" 2>/dev/null | sort -r | head -n 1);
echo "Encontrado $file";
i1=0;i2=0;i3=0;i4=0;
cat $file | while read -r line;
do ind=1;ptos=0;t=${#line};i=0;
while (( i < t ));
do 
 case ${line: ${i}:1} in [0-9]) ptos=$ptos;; .) ptos=$(( ptos + 1 ));; " ") break ;; *) ind=0;;esac;
 i=$(( i + 1 )) ;
done ;
if (( ind == 1 ));then
 case $ptos in
  1) i2=0;i3=0;i4=0;i1=$(( i1 + 1 ));echo "${i1}.${line:${i}}";; 
  2) i3=0;i4=0;i2=$(( i2 + 1 )); echo "${i1}.${i2}.${line:${i}}";; 
  3) i4=0;i3=$(( i3 + 1 ));echo "${i1}.${i2}.${i3}.${line:${i}}";; 
  4) i4=$(( i4 + 1 ));echo "${i1}.${i2}.${i3}.${i4}.${line:${i}}" ;
 esac;
else echo "$line";fi;done;
}

lastFiles () { 
getNow;getToday;
read -p "Dias Antes (0=ninguno) ? " dias;clear;echo -e "Dias ${dias}";d=0;while (( d <= dias ));do ahora=$(date +"%Y-%m-%d-%H-%M-%S");destino1="${ahora}destino1.txt";fecha=$(date -d -${d}days +"%Y-%m-%d");
echo -e "Buscando archivos en $fecha";while read -r line;do file=$(basename ${line});folder=$(dirname ${line});folder2=$(basename ${folder});echo -e "$file ($folder2)" >>${destino1};done < <(find . -type f -iname "${fecha}*" 2>/dev/null | sort -r);cat $destino1 2>/dev/null | sort -r;rm ${destino1} 2>/dev/null;d=$(( d + 1 ));done; 
}

AbrirArchHoy () { 
getNow;getToday;
hoy=$(date +"%Y-%m-%d");
ahora=$(date +"%Y-%m-%d-%H-%M-%S");
dest1="${ahora}lista1.csv";
dest1=$(echo $dest1 | sed 's/-//g' );
dest11=$(echo $dest1 | sed 's/\.csv//g' );
basefolder="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync";
logcambios=$(find $basefolder -type f -iname "${hoy}*logcambios*" 2>/dev/null | sort | tail -n 1 );
logcambiosf=$(basename "$logcambios");
echo -e "Abrir archivos txt Hoy - (R) Jose Zuccoli";
echo -e "Lista:	${dest1}
basefolder	${basefolder}
logcambios	${logcambiosf}"
i=1;
histTerminos=$(find $basefolder -type f -iname "*histTerm.txt" 2>/dev/null | sort | tail -n 1 );
term="";texto=""
if [[ "$histTerminos" != "" ]];then 
  term=$(cat ${histTerminos} | tail -n 1 );
  term=${term:36}
  histTerminosf=$(basename "${histTerminos}")
  echo "Obteniendo de ${histTerminosf} Ultimo Termino: ${term}";
  texto=" [ENTER=${term}]";
fi
read -p "Termino${texto} ? " busq;
if [[ "${busq}" == "" && "${term}" != "" ]];then 
    echo -e "Seleccion por defecto
Utilizamos ${term} como busqueda"
    busq="${term}"
  else
    if [[ "$busq" != "" && "$histTerminos" == "" ]];then 
      echo "Creando terminos ${hoy}histTerm.txt";
      histTerminos="${hoy}histTerm.txt"
      echo "${ahora}:Termino buscado ${busq}" >>${histTerminos}
    fi
    if [[ "$busq" != "" && "$histTerminos" != "" ]];then
      echo "Agregando terminos ${hoy}histTerm.txt";
      echo "busq ${busq} histTerminos ${histTerminos}"
      echo "${ahora}:Termino buscado ${busq}" >>${histTerminos}
    fi
    if [[ "${busq}" == "" ]];then echo -e "Sin termino de busqueda";fi
fi
echo "Opcion;Archivo;ruta" >>${dest1};
while read -r file; do
  file2=$(basename ${file:2})
  echo "${i};${file2};${file:2}" >>${dest1};
  i=$((i+1));
done < <(find . -type f -iname "${hoy}*.txt" 2>/dev/null | grep -v "debris" | sort -r );
echo;
csvsql -d ";" --query "SELECT Opcion,Archivo FROM '${dest11}' " ${dest1} | csvlook;
echo;
read -p "Nro ? " nro;
file=$(csvsql -d ';' --query "SELECT ruta FROM '${dest11}' WHERE Opcion = ${nro}" ${dest1} | tail -n 1 );
echo "${ahora}:nano ${file}" >>${logcambios};
nano ${file};
rm ${dest1}; }

MirarArchHoy () { 
getNow;getToday;
hoy=$(date +"%Y-%m-%d");
ahora=$(date +"%Y-%m-%d-%H-%M-%S");
dest1="${ahora}lista1.csv";
dest1=$(echo $dest1 | sed 's/-//g' );
dest11=$(echo $dest1 | sed 's/\.csv//g' );
basefolder="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync";
logcambios=$(find $basefolder -type f -iname "${hoy}*logcambios*" );
echo -e "Abrir archivos txt Hoy - (R) Jose Zuccoli";
i=1;
read -p "Termino ? " busq;
echo "Opcion;Archivo" >>${dest1};
while read -r file; do
  echo "${i};${file:2}" >>${dest1};
  i=$((i+1));
done < <(find . -type f -iname "${hoy}*.txt" 2>/dev/null | grep -v "debris" | sort -r );
if (( i > 1 )); then
echo;
csvsql -d ";" --query "SELECT * FROM '${dest11}' " ${dest1} | csvlook;
echo;
read -p "Nro ? " nro;
file=$(csvsql -d ';' --query "SELECT Archivo FROM '${dest11}' WHERE Opcion = ${nro}" ${dest1} | sed 's/Archivo//g' );
echo "${ahora}:nano ${file}" >>${logcambios};
cat ${file};
else echo "No existen archivos del dia de hoy"
fi;
rm ${dest1}; }

busqCarpInfoj () {
read -p "Termino ? " busq;
echo "Buscando $busq"
echo "Folder actual $(pwd)"
find . -maxdepth 1 -type f -iname "*${busq}*" 2>/dev/null
i=0;enlace=0;enlace2="";
while read -r file;do cat $file | while read -r line;do if (( enlace == 0 ));then 
find=$(echo "$line" | grep -i "Enlace:");
if [[ "$find" != "" ]];
then enlace=1;fi;i=$((i+1));
else am start -a android.intent.action.VIEW -d "${line}";
break;fi;done;
done < <(find . -maxdepth 1 -type f -iname "*${busq}*" 2>/dev/null | sort );
}


infojobsBusq () {
busq="linkEmpleo.txt";local=$(find /data/data/com.termux/files/home/storage/shared/Download/DownloadSync -maxdepth 1 -type d -iname "*infojobsApp*" 2>/dev/null | sort | tail -n 1 );cd -- "$local";fileInfojobsBusq=$(find . -type f -iname "*${busq}*" 2>/dev/null | sort | tail -n 1 );busqInf=$(cat $fileInfojobsBusq);python3 empinf.py buscar $busqInf
}

infojobsEdit () {
busq="linkEmpleo.txt";local=$(find /data/data/com.termux/files/home/storage/shared/Download/DownloadSync -maxdepth 1 -type d -iname "*infojobsApp*" 2>/dev/null | sort | tail -n 1 );cd -- "$local";fileInfojobsBusq=$(find . -type f -iname "*${busq}*" 2>/dev/null | sort | tail -n 1 );nano ${fileInfojobsBusq};
}

infojobsChoose () {
busq="linkEmpleo.txt";local=$(find /data/data/com.termux/files/home/storage/shared/Download/DownloadSync -maxdepth 1 -type d -iname "*infojobsApp*" 2>/dev/null | sort | tail -n 1 );cd -- "$local";empleo=$(find . -type f -iname "*${busq}*" 2>/dev/null | sort | tail -n 1 );echo "Buscando opBusqEmpleo...";select f in $(find . -maxdepth 1 -type f -iname "*opBusqEmpleo*" 2>/dev/null );do if (( $REPLY == 0 ));then echo "Saliendo...";break;else echo "Elejido $f";cp -- "$f" ${empleo};fi;echo $f;done;
infojobsBusq
}
