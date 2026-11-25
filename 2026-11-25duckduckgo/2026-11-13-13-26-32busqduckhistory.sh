set -u
set -x
export PS4='+ ${BASH_SOURCE}:${LINENO}: ';
set -e

source "$functest";
export log="${ds}/${today}commands.log";

# Función que registra el comando
log_tmux_commands() {
    # Obtener contexto de tmux (Sesión:Ventana). Si no hay tmux, pone "no-tmux"
    local tmux_ctx=$(tmux display-message -p '#S:#W' 2>/dev/null || echo "no-tmux")

    # Obtener el último comando ejecutado del historial
    # history 1 devuelve la última línea; sed elimina el número de línea inicial
    local last_cmd=$(history 1 | sed 's/^[ ]*[0-9]*[ ]*//')

    # Registrar con fecha, contexto y comando
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$tmux_ctx] $last_cmd" >> "$log"
}

# Ejecutar la función antes de cada prompt
export PROMPT_COMMAND="log_tmux_commands"

logfcom () {
      logc="";
      dontsearchdays=15;
      getNow >/dev/null;getToday >/dev/null;
      ayer=0;
      actual=$(pwd);
      busq="logcomandos.txt";findfile="";
      while [[ "$findfile" == "" ]];do
        fecha=$(date -d -${ayer}days +"%Y-%m-%d");
        findfile=$(find $base -type f -iname "${fecha}*${busq}*" 2>/dev/null | grep -v "MEGA" | sort | tail -n 1 );
        if [[ "$findfile" != "" ]];then
          export logc="${findfile}"
          folder=$(dirname "${findfile}");findfile=$(basename "${findfile}");
          if [[ "${findfile:0:10}" != "${today}" ]];then
            cd "${folder}";mv "${findfile}" "${today}${findfile:10}";cd "${actual}";
            export logc=$(echo "${logc}" | sed "s/${findfile}/${today}${findfile:10}/g" );
          fi;
          break;
        fi;
        ayer=$(( ayer + 1 ));
        if (( ayer > dontsearchdays ));then export logc="${ds}/${today}${busq}";findfile="$logc";fi;
      done;
}

# Definir la ruta del archivo de log: Definida como export logc= en logfcom
logfcom;
#export log="$HOME/commands.log"

# Función que registra el comando
log_tmux_commands() {
    # Obtener contexto de tmux (Sesión:Ventana). Si no hay tmux, pone "no-tmux"
    local tmux_ctx=$(tmux display-message -p '#S:#W' 2>/dev/null || echo "no-tmux")

    # Obtener el último comando ejecutado del historial
    # history 1 devuelve la última línea; sed elimina el número de línea inicial
    local last_cmd=$(history 1 | sed 's/^[ ]*[0-9]*[ ]*//')

    # Registrar con fecha, contexto y comando
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$tmux_ctx] $last_cmd" >> "$logc"
}

# Ejecutar la función antes de cada prompt
export PROMPT_COMMAND="log_tmux_commands"
