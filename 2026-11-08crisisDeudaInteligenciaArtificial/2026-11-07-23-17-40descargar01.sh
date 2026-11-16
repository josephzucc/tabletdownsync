while read -r ent;do python3 cnmv_oir.py buscar --entidad "${ent}" --ultimos 15 --descargar --convertir;done < <(echo -e "ACS\nGrenergy\nSolaria\nMerlin\nFerrovial")
