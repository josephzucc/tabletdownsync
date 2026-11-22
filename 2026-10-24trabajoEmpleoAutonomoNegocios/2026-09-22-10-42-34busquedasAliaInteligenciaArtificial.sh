clear
echo -e "\n\n\n\n\n\n\n\n\n\n"
echo -e "2026-09-18-11-01-07iaCrisisFinanciera_iaMilitar_claude_ai.txt\n2026-09-22-09-31-00autonomoMadridAvila_ExperienciaSuiza_claude_ai_adj_contratosPublicosSoberaniaDigitalMadridAvila.txt\n2026-09-18-12-26-59iaOportunidadesNegocios_claude_ai.txt\n2026-09-10-18-14-00consultaProyectoIAparaEmpleos_claude_ai.txt\n2026-09-21-11-35-34articulo_suiza_claude_ai_adj_SuizaEstrategiaPerfilTecnicoProgramador.txt\n2026-09-22-08-25-06articulo_suiza_claude_ai.txt\n2026-09-21-20-58-46articulo_suiza_claude_ai_adj_SuizaPlanAccionVacantesCasoEstudioAleman.txt\n2025-06-26-12-54-58articulo_chatgpt_machineLearningResearchRole.txt" | sort -r | while read -r file;
do
  echo $file
  base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/"
  file2=$(find $base -type f -iname "*${file}*" 2>/dev/null | sort | tail -n 1 )
  file3=$(basename "${file2}")
  line=$(cat "${file2}" | grep -i 'alia')
  if [[ "$line" != "" ]];then echo -e "\n${file3}\n${line}\n";fi
  line=$(cat "${file2}" | grep -i 'salamandra' )
  if [[ "$line" != "" ]];then echo -e "\n${file3}\n${line}\n";fi
  line=$(cat "${file2}" | grep -i 'bsc' )
  if [[ "$line" != "" ]];then echo -e "\n${file3}\n${line}\n";fi
done
