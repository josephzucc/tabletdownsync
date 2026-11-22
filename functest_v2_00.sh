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
  rm -- "*lista1.txt" 2>/dev/null;lista="${now}lista1.txt";
  # echo "${lista}";
  i=1;
  cant=$(find . -maxdepth 1 -type d -iname "${today}*" 2>/dev/null | tail -n 1);
  if [[ "$cant" != "" ]];
    then
      #echo "\$cant no es vacio";
      while read -r file;do j=$(printf "%02d" ${i});echo "${j}. ${file:2}" >>${lista};i=$((i+1));done < <(find . -maxdepth 1 -type d -iname "${today}*" 2>/dev/null | sort -r );cat ${lista};read -p "Numero ? " nro;nro2=$(printf "%02d" ${nro});fd=$(cat ${lista} | grep "^${nro2}\.");fd=${fd:4};echo $fd;echo "${now}:cd -- ${fd}" >>${logcambios};cat "${logcambios}" | tail -n 1;rm "${lista}";cd -- "${fd}"
    else echo "No existen carpetas Actuales";
  fi;
}
