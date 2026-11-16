git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp
cmake -B build -DGGML_NO_OPENMP=ON
cmake --build build -j"$(nproc)"
