[[ "$functest" != "" ]] && source $functest >/dev/null;gn;chdt () { while read -r line;do dt=${line:29:10};de=$(dia="${line:37:2}";case ${line:34:2} in "09") mes="septiembre";; "10") mes="octubre";;esac;echo " ${dia} de ${mes} de 2026");echo ${line} | sed "s/${dt}/${de}/g";done };getIndPie () { if [[ ! -t 0 ]];then text2=$(cat);text1=$(cat indice_pdf.txt | head -n 3);text3=$(cat indice_pdf.txt | tail -n 2);echo -e "$text1\n\n$text2\n\n$text3";fi; };clear;gn;d="${now}indice.txt";echo "Pagina | Nombre Documento" >>$d;cat indice_pdf.txt | cut -c "1-39" | tail +4 | head -n 5 | chdt >>$d;cat $d | csvlook -d '|' | getIndPie >${now}indiceFinal.txt;rm $d
if=$(find . -type f -iname "*indiceFinal*.txt" 2>/dev/null | sort | tail -n 1);

# old method
# python3 $ds/txt2pdf.py ${if};

# limpiamos el txt y lo sobreescribimos, dejando un .bak con el original
python3 2026-10-29-18-42-02txtclean.py ${if} --in-place

# convertimos txt en pdf manteniendo tablas y demas
pandoc -f markdown \
-V lang=es \
-V geometry:margin=2cm \
-o indiceFinal-prueba.pdf \
${if}
