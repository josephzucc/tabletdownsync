#echo -e "165\n170\n171\n172" | while read -r h;do bash *leerhome*.sh $d 20261108 $h;done

find2 () {
while read -r file;do
  t="mv \"${file}\"";
done
}

while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(find $HOME/bop/diag/ -type f -iname "*${2}*${3}*" 2>/dev/null )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(find $HOME/fuentes_ia/diagnostico/ -type f -iname "*${2}*${3}*" 2>/dev/null )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(find $HOME/ree/informes/ -type f -iname "*${2}*${3}*" 2>/dev/null )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(find $HOME/ree/diagnostico/ -type f -iname "*${2}*${3}*" 2>/dev/null )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(find $HOME/ree/descargas/ -type f -iname "*${2}*${3}*" 2>/dev/null )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(find $HOME/subv/diag/ -type f -iname "*${2}*${3}*" 2>/dev/null )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(find diagnostico/ -type f -iname "*${2}*${3}*" 2>/dev/null )

while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(ls $HOME/fuentes_ia/fuentes_ia.log )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(ls $HOME/ree/servidor_salida.log )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(ls $HOME/ree/ree.log )
while read -r file;do b=$(basename "$file");t="mv \"${file}\" \"${1}${b}\"";echo "${t}";eval "${t}";done < <(ls *.log | grep -v "2026" )
