export ast="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/2026-10-23asturias";
export boletin="/data/data/com.termux/files/home/storage/shared/Download/DownloadSync/2026-10-23boletinesOficiales";
cd ${boletin};
python3 bop.py web bopa -c "emergencia social" --limite 40;
cd $ast;
