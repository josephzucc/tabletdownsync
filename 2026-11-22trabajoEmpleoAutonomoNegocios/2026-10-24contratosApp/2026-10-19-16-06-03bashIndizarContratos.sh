base="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync";
source "$base/functest.sh"
init >/dev/null;

getNow >/dev/null;
d="${now}logContratos.txt";
d="2026-10-19-15-57-28logContratos.txt";
echo -e "2022" | while read -r ano;do
  for i in {10..01};do lic="${ano}${i}";getNow >/dev/null;echo -e "${now}: Descargando e indexando ${lic}...." | tee -a $d;python contratos_hacienda.py descargar ${lic} --menores --licitaciones --indexar;done;
done;

getNow >/dev/null;
d="${now}logContratos.txt";
d="2026-10-19-15-57-28logContratos.txt";
echo -e "2021\n2020\n2019\n2018\n2017\n2016" | while read -r ano;do
  for i in {12..01};do lic="${ano}${i}";getNow >/dev/null;echo -e "${now}: Descargando e indexando ${lic}...." | tee -a $d;python contratos_hacienda.py descargar ${lic} --menores --licitaciones --indexar;done;
done;

