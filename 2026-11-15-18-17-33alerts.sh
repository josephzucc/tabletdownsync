termux-notification -t "Alert Title" -c "Something happened in your script!";
termux-notification -l
termux-toast "prueba";
termux-vibrate -d 500;
termux-tts-speak "Attention: Your script has finished";
termux-dialog confirm -t "ALERTA" -i "El proceso ha fallado, ¿deseas reiniciar?"
termux-dialog -l
