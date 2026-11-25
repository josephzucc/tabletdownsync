echo "Modelos de inteligencia artificial instalados... v1.00"
ls ~/modelos

ex="which tesseract";
echo $ex;eval "$ex";

ollama serve &
ollama list;

find $HOME -type f \( -name "*.gguf" -o -name "*.safetensors" -o -name "*.bin" \) 2>/dev/null
