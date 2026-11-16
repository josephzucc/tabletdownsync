txt="ebib_pedir_";
echo "Buscar archivos auxiliares ebib y eliminarlos... (R) Jose Zuccoli 2026";
find . -type f -iname "*${txt}*" 2>/dev/null | while read -r file;do echo "Deleting ${file}";rm $file;done
