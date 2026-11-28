source "$functest";
busq="${1}";
export log="${today}busqfolderds.log"
mylog () { gn;txt="${1}";echo "${now}: ${txt}" >>$log; }
i=0;while (( i < 30 ));do res="";while [[ "$res" == "" ]];do mylog "i ${i}";res="";fecha=$(date -d -${i}days +"%Y-%m-%d");mylog "fecha ${fecha}";res=$(find $ds -type d -iname "${fecha}*${busq}" 2>/dev/null | sort | tail -n 1);mylog "res ${res}";i=$((i+1));done;echo ${res};break;done
