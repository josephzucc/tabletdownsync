(
  cleanup () {
    echo "Caught signal, cleaning up temporary files...";
    exit 0;
  };
  trap cleanup SIGTERM SIGINT;
  a=$(pwd);
  f2=$(dirname "${BASH_SOURCE}");
  cd "$f2";
  source "$functest";
  export pid=$$;
  export salir=0;
  export m=15;
  export t=$((m*60));
  while (( salir == 0 ));do
    gn;
    python3 luzpvpc.py;
    python3 luzpvpc.py --manana;
    sig=$(date -d +${m}minutes +"%Y-%m-%d-%H-%M");
    echo -e "${pid}: Ahora ${now}\nEspero hasta ${sig}";
    sleep $t;wait;
  done;
  cd "$a"; ) & pid1=$!;
