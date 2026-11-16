if (( $# > 0 ));then m="${1}m";else m="15m";fi;
ex="python3 $mailapp -acc yahoo -buscar \"googlealerts-noreply@google.com\" -campos \"de\" -max ${m} -ver-txt -ds -aqui -si-existe sobrescribir";
echo "$ex";eval "$ex";
