source $functest >/dev/null;
gn;
export ev="a=\$(pwd);fold=\$(dirname \"\$ebib\");cd \"\${fold}\";export m=\${min};export now=\$(date +\"%Y-%m-%d-%H-%M-%S\");bash *ebibget*.sh \${min};sig=\$(date -d +\${m}minutes +\"%Y-%m-%d-%H-%M\");echo -e \"\nEsperando \${m} minutos sigte \${sig} hasta \${tmax}\n\";cd \"\${a}\"";
#echo "ev $ev";
baja="${today}ebibbaja.txt";
echo "Para detener crear archivo: $baja";
export min=20;
  (
    echo;i=0;j=10;export mmax=$((j*min));export tmax=$(date -d +${mmax}minutes +"%Y-%m-%d-%H-%M");
    while (( i <= j ));do
      eval "$ev";
      t=$((min*60));
      sleep $t;
      i=$((i+1));
      if [[ -f "${baja}" ]];then echo "Archivo encontrado ${baja}. saliendo de prisa";break;fi;
    done;
    gn;
    echo "saliendo ${now}"; ) & pid=$!;
echo $pid;

