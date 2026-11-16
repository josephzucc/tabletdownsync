ex="python3 aiocr.py ${1} --mode table";
#ex="python3 aiocr.py ${1} --model slmvlm500.gguf --clip mmproj-model-f16.gguf";
echo "$ex";eval "$ex";
