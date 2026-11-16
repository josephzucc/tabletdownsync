source $functest >/dev/null;
gn;
export ev="a=\$(pwd);fold=\$(dirname \"\$ebib\");cd \"\${fold}\";export m=\${min};export now=\$(date +\"%Y-%m-%d-%H-%M-%S\");echo -e \"\nejecutando.. \${now}\";bash *ebibget*;sig=\$(date -d +\${m}minutes +\"%Y-%m-%d-%H-%M\");echo -e \"\nEsperando \${m} minutos \${sig}\n\";cd \"\${a}\"";
#echo "ev $ev"
baja="${today}ebibbaja.txt";
echo "Para detener crear archivo: $baja";
export min=15;
  (
    echo;i=0;j=50;
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

