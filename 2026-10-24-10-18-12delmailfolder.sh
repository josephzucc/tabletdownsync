echo -e "Archivo con funciones auxiliares\nUtilizar source $0"

dma () {
  txt="mails";
  echo "Buscar carpetas mails y subcarpetas vacias y eliminarlas... (R) Jose Zuccoli 2026";
  find . -type d -iname "*${txt}*" 2>/dev/null | while read -r folder;do
    echo -e "Antes de eliminar mails miramos si tiene subcarpetas y estas archivos y\n\t1. Eliminamos archivos.\n\t2. Eliminamos subcarpetas.\n\t3. Eliminamos mails."
    echo -e "\nBuscamos subcarpetas dentro de ${folder}..";
    while read -r subf;do
      echo -e "Encontrada subcarpeta ${subf}... Borrados archivos y carpetas";
      rm $subf/*.*;rm $subf/*;rmdir $subf;
    done < <( find $folder -maxdepth 1 -type d -iname "*" 2>/dev/null | sort )
    echo "Deleting \"${folder}\" . When not empty would show a message...";
    rmdir $folder;
  done;
}

