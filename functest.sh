
delhome () { if (( $# > 0 ));then echo "$1" | sed "s/\/data\/data\/com.termux\/files\/home\///g";elif [[ ! -t 0 ]];then txt=$(cat);echo "$txt" | sed "s/\/data\/data\/com.termux\/files\/home\///g";fi;}
[[ "$functest" == "" ]] && echo "functest verif" && export functest=$(realpath -- "${BASH_SOURCE[0]}") && echo "functest;${functest}" >>${histvar} && echo -e "\tfunctest\t\t$(delhome $functest)";

sea1 () { case $# in 1) busq=${1};base=".";; 2) busq=${1};base=${2};;esac;ex="find ${base} -type f -iname \"*${busq}*\" 2>/dev/null | sort -r ";echo "command: $ex";eval "$ex"; }
dest1 () { gn;if (( $# == 1 ));then d="${now}${1}";else d="${now}log.log";fi; }
seemasters () { find $ds -type f -iname "*maestro*" -printf "%f\n" 2>/dev/null | grep -v -i -E "(pedir|A4)" | sort -r;  }
seemasters2 () { find $ds -type f -iname "*maestro*" 2>/dev/null | grep -v -i -E "(pedir|A4)" | sort -r;  }
cr () { now=$(date +"%Y-%m-%d-%H-%M-%S");d="${now}text.txt";if (( $# > 0 ));then d="${now}${1}.txt";fi;nano "$d"; }
lsi () { shopt -s nocaseglob;find . -maxdepth 1 -type f -iname "*${1}*.*" 2>/dev/null | sort -r;shopt -u nocaseglob; }
openurl () { if (( $# == 1 ));then am start -a android.intent.action.VIEW -d "${1}";else echo "No has especificado url";fi; }
enviow () ( if (( $# == 2 ));then nro=${1};text="${2}";text2=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.stdin.read()))" < ${text});url="https://wa.me/${nro}?text=${text2}";am start -a android.intent.action.VIEW -d "${url}";else echo "No ha especificado nro y texto";fi; )
dpwait () { if (( $# == 2 ));then ( m=${1};ejemp="${2}";if (( $# == 3 ));then cuenta="${3}";else cuenta="madrid";fi;t=$((m*60));sig=$(date -d +${m}minutes +"%Y-%m-%d-%H-%M");echo "Pedir ${ejemp} en ${sig}";sleep $t;wait;dp ${ejemp} ${cuenta};termux-dialog confirm -t "PEDIR $ejemp" -i "${ejemp} pedido"; ) & fi }
fd () { if (( $# > 0 ));then l=" | head -n ${1}";busq="*${2}*";else l="";busq="*";fi;ex="find . -maxdepth 1 -type d -iname \"${busq}\" 2>/dev/null | sort -r${l}";eval "$ex"; }
fdt () { gn;if (( $# > 0 ));then l=" | head -n ${1}";busq="${today}*${2}*";else l="";busq="${today}*";fi;ex="find . -maxdepth 1 -type d -iname \"${busq}\" 2>/dev/null | sort -r${l}";eval "$ex"; }
ff () { if (( $# > 0 ));then l=" | head -n ${1}";busq="*${2}";else l="";busq="*.*";fi;ex="find . -maxdepth 1 -type f -iname \"${busq}\" 2>/dev/null | sort -r${l}";eval "$ex"; }
od () {
  busq="";l="";
  if (( $# > 0 ));then
    #echo $#;
    case $# in 1) busq="*";l="";; 2) busq="*${2}*";l="";; 3) echo $#;busq="*${2}*";l=" | head -n ${3}";;esac;
    days=${1};
  else
    l="";days=0;busq="*";
  fi;
  i=0;
  while (( i <= days ));do
    fecha=$(date -d -${i}days +"%Y-%m-%d");busq2="${fecha}${busq}";
    ex="find . -maxdepth 1 -type d -iname \"${busq2}\" -printf '%f\n' 2>/dev/null | sort -r${l}";echo -e "\n${busq2}";eval "$ex";
    i=$((i+1));
  done;
 }

of () {
  if (( $# > 0 ));then
    days=${1};
    case $# in 1) busq="*";l="";; 2) l="";busq="*${2}";; 3) l=" | head -n ${3}";busq="*${2}";;esac;
  else
    l="";days=0;busq="*.*";
  fi;
  i=0;
  while ((i <= days ));do
    fecha=$(date -d -${i}days +"%Y-%m-%d");busq2="${fecha}${busq}";
    ex="find . -maxdepth 1 -type f -iname \"${busq2}\" -printf '%f\n' 2>/dev/null | sort -r${l}";echo -e "\n${busq2}";eval "$ex";
    i=$((i+1));
  done;
}

of2 () { if (( $# > 0 ));then days=${1};case $# in 1) busq="*";l="";; 2) l="";busq="*${2}";; 3) l=" | head -n ${3}";busq="*${2}";;esac;                        else                                                                                                                       l="";days=0;busq="*.*";                                                                                                fi;                                                                                                                      i=0;                                                                                                                     while ((i <= days ));do                                                                                                    fecha=$(date -d -${i}days +"%Y-%m-%d");busq2="${fecha}${busq}";ex="find . -type f -iname \"${busq2}\" -printf '%f\n' 2>/dev/null | sort -r${l}";echo -e "\n${busq2}";eval "$ex";i=$((i+1));done; }

checkpend () { b () { while read -r file;do c1=$(basename $file);c2=$(basename $(dirname $file));echo "$c1 (${c2})";done  };find $ds -type f -iname "*pendiente*.txt" 2>/dev/null | b | sort; }
checkpend2 () { b () { while read -r file;do c1=$(basename $file);echo "$c1";done };find $ds -type f -iname "*pendiente*.txt" 2>/dev/null | b | sort; }
seesome () { if (( $# > 0 ));then d=$(mktemp);while read -r file;do echo "echo -e \"\\n\\n\\n=====================\\n$file\";less -M \"$file\";" >>$d;done < <(find . -maxdepth 1 -type f -iname "*${1}*" 2>/dev/null | sort -r );bash "$d";rm $d;fi  }

init () {
  echo "ejecutando functest init";
  now=$(date +"%Y-%m-%d-%H-%M-%S");
  export today=$(date +"%Y-%m-%d");
  echo -e "now\t\t${now}\ntoday\t\t${today}"
  export base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  export shar="/data/data/com.termux/files/home/storage/shared/";
  echo -e "base\t\t$(delhome ${ds})\nshar\t\t$(delhome $ds)";

  if [[ "$ebib" != "" ]];then echo -e "\tebib\t\t\tEstablecido desde $(basename ${histvar})";
  elif [[ "$ds" != "" ]] then
    export ebib=$(find $ds -type f -iname "ebib.py" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1);
    export ebibf=$(dirname "$ebib");
    echo "ebib;${ebib}" >>${histvar};
    echo "ebibf;${ebibf}" >>${histvar};
    echo -e "\tebib\t\t\t$(basename ${ebib})";
  else echo -e "\tshar vacio.\t\t\tNo puedo establecer ebib\n";
  fi;
  if [[ "$mailapp" != "" ]];then echo -e "\tmailapp\t\t\tEstablecido desde $(basename ${histvar})";
  elif [[ "$ds" != "" ]] then
    export mailapp=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1);
    echo "mailapp;${mailapp}" >>${histvar};
    echo -e "\tmailapp\t\t\t$(basename ${mailapp})";
  else echo -e "\tshar vacio.\t\t\tNo puedo establecer mailapp\n";
  fi;

  if [[ "$fbapp" != "" ]];then echo -e "\tfbapp\t\t\tEstablecido desde $(basename ${histvar})";
  elif [[ "$ds" != "" ]] then
    export fbapp=$(find $ds -type f -iname "fb.py" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1);
    echo "fbapp;${fbapp}" >>${histvar};
    echo -e "\tfbapp\t\t\t$(basename ${fbapp})";
  else echo -e "\tshar vacio.\t\t\tNo puedo establecer fbapp\n";
  fi;

  if [[ "$carruselapp" != "" ]];then echo -e "\tcarruselapp\t\t\tEstablecido desde $(basename ${histvar})";
  elif [[ "$ds" != "" ]] then
    export carruselapp=$(find $ds -type f -iname "carrusel.py" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1);
    echo "carruselapp;${carruselapp}" >>${histvar};
    echo -e "\tcarruselapp\t\t\t$(basename ${carruselapp})";
  else echo -e "\tshar vacio.\t\t\tNo puedo establecer carruselapp\n";
  fi;

  export fbda="/data/data/com.termux/files/home/fb_datos";
  echo -e "fbda\t\t$(delhome $fbda)";
  export fbdi="/data/data/com.termux/files/home/storage/shared/Download/FBVigia/diagnosticos/"
  echo -e "fbdi\t\t$(delhome $fbdi)";
  export mesa=$(date +"%Y-%m");
  export mess=$(date -d +30days +"%Y-%m");
  echo -e "mesa\t\t$(delhome $mesa)\nmess\t\t$(delhome $mess)";

  if [[ "$gastosm" != "" ]];then echo -e "\tgastosm\t\t\tEstablecido desde $(basename ${histvar})";
  elif [[ "$ds" != "" ]] then
    export gastosm=$(find $ds -type f -iname "${mesa}*gastos.txt" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1);
    if [[ "$gastosm" != "" ]];then
      echo "gastosm;${gastosm}" >>${histvar};
      echo -e "\tgastosm\t\t\t$(basename ${gastosm})";
    else echo "No se ha encontrado gastosm ${mesa}*gastos.tx";
    fi;
  else echo -e "\tshar vacio.\t\t\tNo puedo establecer gastosm\n";
  fi;
  if [[ "$gastoss" != "" ]];then echo -e "\tgastoss\t\t\tEstablecido desde $(basename ${histvar})";
  elif [[ "$ds" != "" ]] then
    export gastoss=$(find $ds -type f -iname "${mess}*gastos.txt" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1);
    echo "${gastoss}"
    if [[ "$gastoss" != "" ]];then
      echo "gastoss;${gastoss}" >>${histvar};
      echo -e "\tgastoss\t\t\t$(basename ${gastoss})";
    else echo "No se ha encontrado gastoss ${mess}*gastos.tx";
    fi;
  else echo -e "\tshar vacio.\t\t\tNo puedo establecer gastoss\n";
  fi;
  checksh="${ds}/2026-11-05-14-08-11checkmail.sh";
}

agastosm () {
  mesa=$(date +"%Y-%m");echo "$mesa $mess";export gastosm=$(find $ds -maxdepth 1 -type f -iname "${mesa}*gastos.txt" 2>/dev/null | sort | tail -n 1 );echo -e "gastosm ${gastosm}\ngastoss ${gastoss}"; 
}

gn () { getNow >/dev/null; }

getNow () {
  now=$(date +"%Y-%m-%d-%H-%M-%S");
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Variable \$now establecida en ${now}";
  echo -e "Variable \$ds establecida en ${ds}";
  if [[ "$ebib" == "" ]];then
    export ebib=$(find ${base} -type f -iname "ebib.py" | grep -v "MEGA" | tail -n 1);
    echo -e "Variable \$ebib establecida en ${ebib}";
  fi;
  if [[ "$mailapp" == "" ]];then
    export mailapp=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | head -n 1 );
    echo -e "Variable \$mailapp establecida en ${mailapp}";
  fi;
  if [[ "$carruselapp" == "" ]];then
    export carruselapp=$(find ${base} -type f -iname "carrusel.py" 2>/dev/null | grep -v "MEGA" | head -n 1 );
    echo -e "Variable \$carruselapp establecida en ${carruselapp}";
  fi;
 }

getn () {
  getNow >/dev/null;
  getToday >/dev/null;
}

getToday () {
  today=$(date +"%Y-%m-%d");
  echo -e "Variable \$today establecida en ${today}";
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Variable \$ds establecida en ${ds}";
  if [[ "$ebib" == "" ]];then
    ebib=$(find ${base} -type f -iname "ebib.py" | grep -v "MEGA" | tail -n 1);
    echo -e "Variable \$ebib establecida en ${ebib}";
  fi;
  if [[ "$mailapp" == "" ]];then
    mailapp=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | head -n 1 );
    echo -e "Variable \$mailapp establecida en ${mailapp}";
  fi;
  if [[ "$carruselapp" == "" ]];then
    carruselapp=$(find ${ds} -type f -iname "carrusel.py" 2>/dev/null | grep -v "MEGA" | head -n 1 );
    echo -e "Variable \$carruselapp establecida en ${carruselapp}";
  fi;
 }

getTom () {
  if (( $# > 0 ));then i=${1};else i=1;fi;tom=$(date -d +${i}days +"%Y-%m-%d"); 
}
getYes () { if (( $# > 0 ));then i=${1};else i=1;fi;yes=$(date -d -${i}days +"%Y-%m-%d"); }

gd () {
  out="/dev/stdout";
  if (( $# == 1 ));then case ${1} in "s") out="/dev/null";;esac;fi;
  logfcha2 >$out;
  init >${out};
}

checkmail () {
  source "$functest" >/dev/null;gn;if (( $# > 0 ));then imail=${1};fi;
  stop="${today}mailstop.txt";stopf=$(pwd);
  echo -e "Si deseas parar\ncrea archivo ${stop} en\n${stopf}";
  todas="yahoo gmx consdigg busqp rbrtmichun josech jjzu12 ciberseguridadconsultor ciberseguridadmadridnorte govsif2";
  a=$(pwd);b=$(dirname "$mailapp");cd "$b";
  case $imail in
    "" | 1) cuentas=" gmx ";export imail=2;; 2) cuentas=" gmx yahoo ";export imail=3;;
    3) cuentas=" ${todas} ";export imail=1;;
  esac;
  ex="python3 ${mailapp} -acc ${cuentas} -solo-nuevos -ver";eval "$ex";cd "$a";
}

m () { while read -r file;do cat "$file" | more ;done; }
lastmail () { ls ${today}*correos.txt | sort | tail -n 1 | m; }
chkm () { if (( $# > 0 ));then type=${1};else type=1;fi;gn;d="${now}correos.txt";checkmail ${type} >>${d};lastmail; }

mailg () {
  local qty=10;
  init >/dev/null;
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  correopy=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | head -n 1);
  if (( $# > 0 ));then qty=${1};fi;
  clear;python3 ${mailapp} -acc gmx -last $qty -ver-lista;
 }

mailgetid () {
  # bash mailgetid -a g -b ${id}	 : cuenta gmx busca ${id} con ver
  # bash mailgetid -a gy -b ${id} -d tpd : cuentas gmx y yahoo busca ${id} con ver-txt, ver-pdf y descargar
  #                                        ver-pdf -cabecera y descargar -adjuntos -si-existe sobrescribir
  #!/usr/bin/env bash

while getopts ":a:d:b:" opt; do
  case "$opt" in
    a) a_value=$OPTARG ;;
    d) d_value=$OPTARG ;;
    b) b_value=$OPTARG ;;
    :)
      printf 'Option -%s requires a value\n' "$OPTARG" >&2
      exit 2
      ;;
    \?)
      printf 'Unknown option: -%s\n' "$OPTARG" >&2
      exit 2
      ;;
  esac
done

printf 'a = %s\nd = %s\nb = %s\n' "$a_value" "$d_value" "$b_value";
a_value="";d_value="";b_value="";

}

mailgtxt () {
  if (( $# == 1 ));then
    id=${1};
    init >/dev/null;
    python3 $mailapp -acc gmx -ver-txt ${id} -ds -aqui;
  elif [[ ! -t 0 ]]; then
    init >/dev/null;
    id=$(cat); # Suponemos que en cat habra 1 solo id, no varios
    python3 $mailapp -acc gmx -ver-txt ${id} -ds -aqui;
  else echo "Sin id";
  fi;
}

mailgy () { local qty=10;init >/dev/null;base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";correopy=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | head -n 1);if (( $# > 0 ));then qty=${1};fi;clear;python3 ${correopy} -acc gmx yahoo -last $qty -ver-lista; }
mailall () { local qty=10;init >/dev/null;base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";correopy=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | head -n 1);if (( $# > 0 ));then qty=${1};fi;clear;python3 ${correopy} -acc todas -last $qty -ver-lista; }
mailch () { local qty=10;init >/dev/null;acc="josech";base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";correopy=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | head -n 1);if (( $# > 0 ));then qty=${1};fi;clear;python3 ${correopy} -acc ${acc} -last $qty -ver-lista; }
mailpi () { local qty=10;init >/dev/null;acc="busqp";base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";correopy=$(find $ds -type f -iname "correo.py" 2>/dev/null | grep -v "MEGA" | head -n 1);if (( $# > 0 ));then qty=${1};fi;clear;python3 ${correopy} -acc ${acc} -last $qty -ver-lista; }

logfcha2 () {
  log="";
  getNow >/dev/null;getToday >/dev/null;base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Variable \$now establecida en ${now}\nVariable \$today establecida en ${today}\nVariable \$ds establecida en ${ds}";
  ayer=0;echo -e "Actualizacion fecha logcambios";actual=$(pwd);
  echo "Folder actual ${actual}";
  busq="logcambios";findfile="";
  while [[ "$findfile" == "" ]];do
    fecha=$(date -d -${ayer}days +"%Y-%m-%d");echo "Buscando ${busq} en ${fecha}";
    findfile=$(find $ds -type f -iname "${fecha}*${busq}*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
    if [[ "$findfile" == "" ]];then
      echo "No encontrado ${busq} en ${fecha}....";
    else
      log="${findfile}"
      echo "\$findfile ${findfile}"
      folder=$(dirname ${findfile});findfile=$(basename ${findfile});
      echo -e "Encontrado ${busq} en ${fecha}\nCambio Fecha de ${findfile} a ${today}${findfile:10}";
      if [[ "${findfile:0:10}" != "${today}" ]];then
        echo "cambiar \"${findfile:0:10}\" \"${today}\""
        cd ${folder};mv ${findfile} ${today}${findfile:10};cd ${actual};
        log=$(echo ${log} | sed "s/${findfile}/${today}${findfile:10}/g" );
      else echo "logcambios ya es actual";
      fi;
      break;
    fi;
    ayer=$(( ayer + 1 ));
  done;
}

logfcha2 >/dev/null;

logfcha () {
  getNow;getToday;clear;ayer=0;
  echo -e "Actualizacion fecha logcambios";actual=$(pwd);
  log="";
  echo "Folder actual ${actual}";
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  busq="logcambios";findfile="";
  while [[ "$findfile" == "" ]];do
    fecha=$(date -d -${ayer}days +"%Y-%m-%d");
    echo "Buscando ${busq} en ${fecha}";
    findfile=$(find $ds -type f -iname "${fecha}*${busq}*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
    if [[ "$findfile" == "" ]];then
      echo "No encontrado ${busq} en ${fecha}....";
    else
      log="${findfile}";echo "\$findfile ${findfile}";
      folder=$(dirname ${findfile});
      findfile=$(basename ${findfile});
      echo -e "Encontrado ${busq} en ${fecha}\nCambio Fecha de ${findfile} a ${today}${findfile:10}";
      if [[ "${findfile:0:10}" != "${today}" ]];then
        echo "cambiar \"${findfile:0:10}\" \"${today}\""
        cd ${folder};mv ${findfile} ${today}${findfile:10};cd ${actual};
        log=$(echo ${log} | sed "s/${findfile}/${today}${findfile:10}/g" );
      else echo "logcambios ya es actual";fi;
      break;
    fi;
    ayer=$(( ayer + 1 ));
  done;
}

ahoy () {
  gd;
  clear;
  echo -e "Cambiar Archivo hoy - (R) Jose Zuccoli";
  read -p "Termino ? " busq;
  logcambios=$(find ${base} -maxdepth 1 -type f -iname "*logcambios*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
  echo "Logcambios: ${logcambios}";
  lista1="${now}lista1.txt";
  i=1;
  while read -r file;do i=$(printf "%02d" ${i});echo "${i}. ${file:2}" >>${lista1};i=$(( i + 1 ));done < <(find . -maxdepth 1 -type f -iname "*${busq}*" 2>/dev/null | grep -v "MEGA" | sort -r );
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

choy () {
  d=1;
  today=$(date +"%Y-%m-%d");now=$(date +"%Y-%m-%d-%H-%M-%S");
  echo "$today $now";
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Cambiar Carpeta hoy - (R) Jose Zuccoli";
  case $# in
    1) busq="${1}";;
    0) read -p "Termino ? " busq;;
  esac
  logcambios=$(find ${base} -maxdepth 1 -type f -iname "*logcambios*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
  echo "Logcambios: ${logcambios}";
  lista1="${now}lista1.txt"; rm -- "$lista1" 2>/dev/null;
  i=1;
  echo "Busqueda ${busq}";
  while read -r file;do i=$(printf "%02d" ${i});echo "${i}. ${file:2}" >>${lista1};
    i=$(( i + 1 ));done < <(find . -maxdepth 1 -type d -iname "*${busq}*" 2>/dev/null | grep -v "MEGA" | sort -r );
  if (( i == 2 ));then nro=1;
  else cat ${lista1};read -p "Numero ? " nro;
  fi
  nro=$( printf "%02d" ${nro} );
  file=$(cat ${lista1} | grep "^${nro}.");
  file=${file:4};
  file2="${today}${file:10}";
  echo "${now}:mv $file $file2" >>${logcambios};
  cat ${logcambios} | tail -n 1;
  mv -- $file $file2;
  rm $lista1;
}

cact () {
  #!/usr/bin/env bash
  # -d 1:		Nivel 1 de busqueda ( solo aqui )
  # -a a:		Sin nivel de busqueda (todo)
  # -p "texto"		Elejimos solo las carpetas indicadas en texto.
  #			Si da como resultad solo 1 carpeta, cambiamos automaticamente a esa.
  getNow >/dev/null;getToday >/dev/null;
  echo "$now $today";
  maxdepth="-maxdepth 1";
  busq="${today}"
  while getopts ":d:p" opt; do
    case "$opt" in
      d) dval="$OPTARG";case $dval in [0-9]) maxdepth="-maxdepth ${dval}";; "a") maxdepth="";;esac;;
      p) pval="$OPTARG";busq="${busq}*${pval}*";;
    esac;
  done;
  case $# in
    1) busq="${busq}*${1}";;
  esac
  cacthist="${ds}/${today}cacthist.txt";
  [[ -f "$cacthist" ]] && ( echo "Creo historico cact $(basename ${cacthist})..." ) || echo "Historico acact existe $(basename ${cacthist})...";
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/";
  echo -e "Carpeta Actual Hoy - (R) Jose Zuccoli";
  logcambios=$(find ${base} -maxdepth 1 -type f -iname "*logcambios*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
  echo "Logcambios: $(basename ${logcambios})";
  rm -- "*lista1.txt" 2>/dev/null;lista="${now}lista1.txt";i=1;
  cant=$(find . ${maxdepth} -type d -iname "${busq}*" 2>/dev/null | grep -v "MEGA" | wc -l);
  case $cant in
  1)
     c=$(find . ${maxdepth} -type d -iname "${busq}*" 2>/dev/null | grep -v "MEGA");
     echo "Solo existe 1 carpeta ${busq}. Cambio a ella ${c:2}";
     cd "${c:2}";
     # Agrego a historico
     gn;echo "${now};${busq};${c:2}" >>${cacthist};
     ;;
  0) echo "No existen carpetas Actuales ${busq}";;
  *)
    while read -r file;do
      j=$(printf "%02d" ${i});
      echo "${j}. ${file:2}" >>${lista};
      i=$(( i + 1 ));
    done < <(find . -maxdepth 1 -type d -iname "${busq}*" 2>/dev/null | grep -v "MEGA" | sort -r );
    cat "${lista}";
    read -p "Numero ? " nro;
    # si escribe 0 (cero) salimos
    if (( nro != 0 ));then 
      nro2=$( printf "%02d" ${nro} );
      file=$(cat ${lista} | grep "^${nro2}\.");
      file=${file:4};
      # Agrego a historico
      gn;echo "${now};${busq};${file}" >>${cacthist};
      echo "${now}:cd $file" >>${logcambios};
      cat ${logcambios} | tail -n 1;
      rm -- "${lista}";
      cd -- "$file";
    else echo "Saliendo sin eleccion...";
    fi;
    ;;
  esac;
}

readflags () {
#!/usr/bin/env bash

while getopts ":d:e:" opt; do
  case "$opt" in
    d) d_value=$OPTARG ;;
    e) e_value=$OPTARG ;;
    :)
      printf 'Option -%s requires a value\n' "$OPTARG" >&2
      exit 2
      ;;
    \?)
      printf 'Unknown option: -%s\n' "$OPTARG" >&2
      exit 2
      ;;
  esac
done

printf 'd = %s\ne = %s\n' "$d_value" "$e_value"
}

confebiblio () { nano /data/data/com.termux/files/home/.config/ebib/cuentas.json; }

cambIndTareas () { hoy=$(date +"%Y-%m-%d");
file=$(find . -type f -iname "${hoy}tareasgenera*" 2>/dev/null | grep -v "MEGA" | sort -r | head -n 1);
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
echo -e "Buscando archivos en $fecha";while read -r line;do file=$(basename ${line});folder=$(dirname ${line});folder2=$(basename ${folder});echo -e "$file ($folder2)" >>${destino1};done < <(find . -type f -iname "${fecha}*" 2>/dev/null | grep -v "MEGA" | sort -r);cat $destino1 2>/dev/null | sort -r;rm ${destino1} 2>/dev/null;d=$(( d + 1 ));done; 
}

aarchhoy () { 
getNow;getToday;
hoy=$(date +"%Y-%m-%d");
ahora=$(date +"%Y-%m-%d-%H-%M-%S");
dest1="${ahora}lista1.csv";
dest1=$(echo $dest1 | sed 's/-//g' );
dest11=$(echo $dest1 | sed 's/\.csv//g' );
basefolder="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync";
logcambios=$(find $ds -type f -iname "${hoy}*logcambios*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
logcambiosf=$(basename "$logcambios");
echo -e "Abrir archivos txt Hoy - (R) Jose Zuccoli";
echo -e "Lista:	${dest1}
ds	${ds}
logcambios	${logcambiosf}"
i=1;
histTerminos=$(find $ds -type f -iname "*histTerm.txt" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
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
done < <(find . -type f -iname "${hoy}*.txt" 2>/dev/null | grep -v "MEGA" | grep -v "debris" | sort -r );
echo;
csvsql -d ";" --query "SELECT Opcion,Archivo FROM '${dest11}' " ${dest1} | csvlook;
echo;
read -p "Nro ? " nro;
file=$(csvsql -d ';' --query "SELECT ruta FROM '${dest11}' WHERE Opcion = ${nro}" ${dest1} | tail -n 1 );
echo "${ahora}:nano ${file}" >>${logcambios};
nano ${file};
rm ${dest1}; }

marchhoy () { 
getNow;getToday;
hoy=$(date +"%Y-%m-%d");
ahora=$(date +"%Y-%m-%d-%H-%M-%S");
dest1="${ahora}lista1.csv";
dest1=$(echo $dest1 | sed 's/-//g' );
dest11=$(echo $dest1 | sed 's/\.csv//g' );
basefolder="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync";
logcambios=$(find $ds -type f -iname "${hoy}*logcambios*" | grep -v "MEGA" );
echo -e "Abrir archivos txt Hoy - (R) Jose Zuccoli";
i=1;
read -p "Termino ? " busq;
echo "Opcion;Archivo" >>${dest1};
while read -r file; do
  echo "${i};${file:2}" >>${dest1};
  i=$((i+1));
done < <(find . -type f -iname "${hoy}*.txt" 2>/dev/null | grep -v "MEGA" | grep -v "debris" | sort -r );
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
find . -maxdepth 1 -type f -iname "*${busq}*" 2>/dev/null | grep -v "MEGA";
i=0;enlace=0;enlace2="";
while read -r file;do cat $file | while read -r line;do if (( enlace == 0 ));then
find=$(echo "$line" | grep -i "Enlace:");
if [[ "$find" != "" ]];
then enlace=1;fi;i=$((i+1));
else am start -a android.intent.action.VIEW -d "${line}";
break;fi;done;
done < <(find . -maxdepth 1 -type f -iname "*${busq}*" 2>/dev/null | grep -v "MEGA" | sort );
}


infojobsBusq () {
busq="linkEmpleo.txt";local=$(find /data/data/com.termux/files/home/storage/shared/Download/DownloadSync -maxdepth 1 -type d -iname "*infojobsApp*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );cd -- "$local";fileInfojobsBusq=$(find . -type f -iname "*${busq}*" 2>/dev/null | sort | tail -n 1 );busqInf=$(cat $fileInfojobsBusq);python3 empinf.py buscar $busqInf
}

infojobsEdit () {
busq="linkEmpleo.txt";local=$(find /data/data/com.termux/files/home/storage/shared/Download/DownloadSync -maxdepth 1 -type d -iname "*infojobsApp*" 2>/dev/null | sort | tail -n 1 );cd -- "$local";fileInfojobsBusq=$(find . -type f -iname "*${busq}*" 2>/dev/null | sort | tail -n 1 );nano ${fileInfojobsBusq};
}

infojobsChoose () {
busq="linkEmpleo.txt";local=$(find /data/data/com.termux/files/home/storage/shared/Download/DownloadSync -maxdepth 1 -type d -iname "*infojobsApp*" 2>/dev/null | sort | tail -n 1 );cd -- "$local";empleo=$(find . -type f -iname "*${busq}*" 2>/dev/null | sort | tail -n 1 );echo "Buscando opBusqEmpleo...";select f in $(find . -maxdepth 1 -type f -iname "*opBusqEmpleo*" 2>/dev/null );do if (( $REPLY == 0 ));then echo "Saliendo...";break;else echo "Elejido $f";cp -- "$f" ${empleo};fi;echo $f;done;
infojobsBusq
}

getMap () {
  echo "getMap - Buscar Ciudad en Mapa - (R) Jose Zuccoli";
  echo -e "Introduce Pueblo con ubicacion provincia y pais: Por ej La Adrada, Avila, Espana";
  if (( $# > 0 ));then busq="${1}";else busq="La Adrada, Avila";fi;
  echo "Busco ciudad/pueblo ${busq}";
  am start -a android.intent.action.VIEW -d "https://duckduckgo.com/?t=fpas&q=${busq}&iaxm=maps";
}

chscreen () {
f=${1};
txt=${2};

g=$(basename "$f");
w=${g:11:8}
g=$( echo ${g} | sed "s/Screenshot_//g" | sed "s/${w}_/${w}/g" );
h="${g:0:4}-${g:4:2}-${g:6:2}-${g:8:2}-${g:10:2}-${g:12:2}${txt}"
echo "$h"
}

wlog () {

 if (( $# > 0 ));then
   gd s;
   txt=${1};
   echo "${now}:${txt}" >>${log};
 elif [[ ! -t 0 ]]; then
   txt=$(cat);
   gd s;
   if [[ $txt == *$'\n'* ]]; then
     echo -e "$txt" | while read -r line;do wlog "$line";done;
   else
     echo "${now}:${txt}" >>${log};
   fi;
 fi;
}

wolog () {
 if (( $# > 0 ));then txt="${1}";wlog "${txt}";echo "$txt";fi;
}

welog () {
 if (( $# > 0 ));then txt="${1}";wlog "${txt}";echo "$txt" | bash;fi;
}

dav () { txt="python3 $ebib -acc castillaleon -lista \"diarioavila\" -pedir;";echo $txt;echo $txt | bash; }
dre () { txt="python3 $ebib -acc todas -resumen;";echo $txt;echo $txt | bash; }
dp () { pedido="";cuentas="todas";if (( $# > 0 ));then pedido="${1}";if (( $# == 2 ));then cuentas="${2}";fi;txt="python3 $ebib -acc ${cuentas} -lista \"${pedido}\" -pedir;";echo "Pidiendo $txt";echo $txt | bash;else echo "No se ha especificado periodico";fi; }

se () {
busq="";if (( $# > 0 ));then busq="${1}";fi;paso=1;while (( paso < 3 ));do echo -e "*${busq}*.zip\n*${busq}*.html\n*man*${busq}*.txt\n*${busq}*man*.txt\n*${busq}*.xpi\n*${busq}*.py" | while read -r file;do case $paso in 1) find .. -maxdepth 1 -type f -iname "*${file}*" 2>/dev/null | grep -v "MEGA";; 2) find .. -maxdepth 1 -type f -iname "*${file}*" 2>/dev/null | grep -v "MEGA" | while read -r file; do mv "${file}" .;done;out=3;;esac;done;read -t 5 -p "paso [${paso}] 2=eje,3=fin ? " paso;if [[ "$paso" == "" ]];then paso=1;fi;echo;done;
busq="";if (( $# > 0 ));then busq="${1}";fi;paso=1;while (( paso < 3 ));do echo -e "*${busq}*.zip\n*${busq}*.html\n*man*${busq}*.txt\n*${busq}*man*.txt\n*${busq}*.xpi\n*${busq}*.py" | while read -r file;do case $paso in 1) find ../.. -maxdepth 1 -type f -iname "*${file}*" 2>/dev/null | grep -v "MEGA";; 2) find ../.. -maxdepth 1 -type f -iname "*${file}*" 2>/dev/null | grep -v "MEGA" | while read -r file; do mv "${file}" .;done;out=3;;esac;done;read -t 5 -p "paso [${paso}] 2=eje,3=fin ? " paso;if [[ "$paso" == "" ]];then paso=1;fi;echo;done; }
se2 () { busq="";if (( $# > 0 ));then busq="${1}";fi;paso=1;while (( paso < 3 ));do echo -e "*${busq}*.zip\n*${busq}*.html\n*man*${busq}*.txt\n*${busq}*man*.txt\n*${busq}*.xpi\n*${busq}*.py" | while read -r file;do case $paso in 1) find ../../.. -maxdepth 1 -type f -iname "*${file}*" 2>/dev/null | grep -v "MEGA";; 2) find ../../.. -maxdepth 1 -type f -iname "*${file}*" 2>/dev/null | grep -v "MEGA" | while read -r file;do mv "${file}" .;done;out=3;;esac;done;read -t 5 -p "paso [${paso}] 2=eje,3=fin ? " paso;if [[ "$paso" == "" ]];then paso=1;fi;echo;done; }

busqia () {

if (( $# > 0 ));
  then txt="$1";
  else txt="artificiall intelligence\ninteligencia artificial";
fi;
echo -e "Busco:\n${txt}";
gd s;
sp="";
t=114;
for ((r=0; r <= t; r++));do sp="${sp} ";done;
echo -e "$txt" | while read -r busq;do
  while read -r file;do
    file2=$(basename "$file" | cut -c 1-114);s=${#file2};s=$((114-s));file2="${file2}${sp:0: ${s}}";
    echo -ne "$file2"\\r;line="";
    while read -r line;do echo -e "\n${line}\n";sleep 30;done < <(cat $file | grep -i "${busq}");
    done < <(find ${base} -type f -iname "2026-*.txt" 2>/dev/null | grep -v "MEGA");
  done;
}

carr1 () {
  folder=$(pwd);
  init >/dev/null;
  python3 $carruselapp --folder ${folder} --background;
}

carrstop () { init >/dev/null;python3 $carruselapp --stop; }

form5 () {

#set -u;set -x;export PS4='+ ${BASH_SOURCE}:${LINENO}: ';set -e;

concepto="*********** RESTO ***********";
clear;
ingresos="${1}";
getNow >/dev/null;
totp=0;
d="${2}";

if [[ ! $1 =~ ^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then
    echo "Not a number" >&2; return 1;
fi

if [[ -z $d || $d == */* ]];then
  echo "Expected a filename without a path $d" >&2;return 1;
fi

if [[ ! -f "$d" ]];then
    echo "File not exists $d" >&2; return 1;
fi

r=$(echo $d | sed "s/\-//g" );
p=$(echo $r | sed "s/\.txt//g" );

q="${now}tmp1.txt";q=$(echo $q | sed "s/\-//g" );
cp -- "$d" "$r" || { echo "could not copy $d $r" >&2;return 1; }

# Ingreso el resto
tot=$(csvsql --query "SELECT SUM(IMPORTE) AS TOT FROM '${p}' ORDER BY IMPORTE DESC" ${r} | grep -v "TOT");
if [[ ! $tot =~ ^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then echo "tot is not a number" >&2;return 1;fi
importe=$(printf "%01.2f" $(echo "scale=2;(${ingresos}-${tot})" | bc ));
if [[ ! $importe =~ ^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then echo "importe is not a number" >&2;return 1;fi
echo "$concepto;$importe" >>${r};

tot=0;
csvsql --query "SELECT * FROM '${p}' ORDER BY IMPORTE DESC" ${r} >${q}
rm "$r" 2>/dev/null

s="${now}tmp2.txt";
s=$(echo $s | sed "s/\-//g" );
e=$(echo $s | sed "s/\.txt//g" );

while IFS="," read concepto importe;do
  if [[ "${concepto}" == "CONCEPTO" ]];then echo "CONCEPTO;IMPORTE;PORC;TOTP;TOT" >>${s};
  else
    porc=$(printf "%01.2f" $(echo "scale=2;(${importe}*100)/${ingresos}" | bc) );
    if [[ ! $porc =~ ^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then echo "porc is not a number" >&2;return 1;fi
    tot=$(printf "%01.2f" $(echo "scale=2;(${tot}+${importe})" | bc ));
    if [[ ! $tot =~ ^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then echo "tot is not a number" >&2;return 1;fi
    totp=$(printf "%01.2f" $(echo "scale=2;((${tot}*100)/${ingresos})" | bc) );
    if [[ ! $totp =~ ^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then echo "totp is not a number" >&2;return 1;fi
    echo "$concepto;$importe;$porc;$totp;$tot" >>${s};
  fi;
done < <( cat ${q} );

# Show the header and first few records as csvkit parses them
csvlook --snifflimit 0 -d ',' $s | head

# Check for rows whose field count differs from the header
csvclean --length-mismatch $s >/dev/null
csvclean -d ';' --length-mismatch "$s" >/dev/null

# Inspect the reported physical lines (for example, around line 11)
nl -ba $s | sed -n '8,14p'

# Show the header names
csvcut -n $s

clear;echo -e "\n\n\n==================================================\n\n\nReporte generado el ${now}";
csvsql --snifflimit 0 -d ';' --query "SELECT * FROM '${e}' ORDER BY IMPORTE DESC" $s | csvlook -d ',';
rm "$t" 2>/dev/null;
rm "$q" 2>/dev/null;
rm "$s" 2>/dev/null;

}

tot () {
while read -r f;do echo $f;g=$(echo $f | sed "s/\-//g" );h=$(echo $g | sed "s/\.txt//g" );cp "$f" $g;csvsql --query " SELECT SUM(IMPORTE) FROM '${h}' " $g;rm $g;done < <(ls ${today}*gastos*.txt);
}

#to_epoch funcion auxiliar para restar o sumar fechas
to_epoch() { date -d "${1:0:10} ${1:11:2}:${1:14:2}:${1:17:2}" +%s; }
#to_epoch2 resta fechas d1="2026-10-19-16-05-04";d2="2026-10-19-15-57-29": devuelve cantidad de segundos
to_epoch2() { diff=$(( $(to_epoch "$2") - $(to_epoch "$1") ));(( diff < 0 )) && diff=$(( -diff ));echo $diff; }

busqsc () {
if (( $# > 1 ));then horai="${1}";texto="${2}";horaf="${1}";(( $# == 3 )) && horaf="${3}";fi;
oggi=$(date +"%Y%m%d");
while (( "${horai}" <= "${horaf}" ));do
  echo "Buscando ${oggi}_${horai}....";
  find $shar -type f -iname "*${oggi}_${horai}*" 2>/dev/null | grep -v "MEGA" | while read -r file;do
    d=$(chscreen "$file" "${texto}");
    echo "mv \"${file}\" $d";
    mv "$file" $d;
  done;
  horai=$((horai+1));
done
}

busqsc2 () {
if (( $# > 1 ));then horai="${1}";texto="${2}";horaf="${1}";(( $# >= 3 )) && day=${3};(( $# == 4 )) && horaf="${4}";fi;
while (( ${horai} <= ${horaf} ));do
  echo "Buscando ${day}_${horai}....";
  find $shar -type f -iname "*${day}_${horai}*" 2>/dev/null | grep -v "MEGA" | while read -r file;do
    d=$(chscreen "$file" "${texto}");
    echo "mv \"${file}\" $d";
    mv "$file" $d;
  done;
  horai=$((horai+1));
done
}

vertexto () {
read -p "Nombre manual (${m}) ? " file;
if [[ "$file" == "" ]];then file=${m};fi;
set -- "${file}";
if (( $# > 0 ));then 
  i=1;
  file=${1};
  out="";
  while [[ "$out" == "" ]];do j=$((j+30));cat ${file} | sed -n "${i},${j}p";i=$((i+30));read -p "out (${i}) [vacio] ? " out;done;
else echo "no se ha indicado archivo a abrir";
fi;
}

busqdiag () {
s="${ds}*search*.sh";
while read -r file;do
  b=$(basename "$file");
  c="${b}";
  text="${b}";
  search="2026";
  i=$(bash "${s}");
  echo "${i}";
  echo "${b}";
  #echo "${b: ${i} :15 }";
  b=$(echo ${b: ${i}:15} | sed "s/_//g");
  d="${b:0:4}-${b:4:2}-${b:6:2}-${b:8:2}-${b:10:2}-${b:12:2}${c:0: ${i}}${c: -5:5}";
  t="mv \"${file}\" \"${d}\"";
  #echo "${t}";
  eval "${t}";
done;
}
