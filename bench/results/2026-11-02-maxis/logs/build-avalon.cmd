@echo off
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 exit /b 1
set "CUDA_PATH=<run-root>\build-local\cuda"
set "PATH=%CUDA_PATH%\bin;<run-root>\build-local\tooling\bin;%PATH%"
set "TEMP=<run-root>\build-local\temp"
set "TMP=%TEMP%"
"<run-root>\build-local\tooling\cmake\data\bin\cmake.exe" -S build-local\Avalon -B build-local\avalon-build -G Ninja -DCMAKE_MAKE_PROGRAM="<run-root>\build-local\tooling\bin\ninja.exe" -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON -DCUDAToolkit_ROOT="%CUDA_PATH%"
if errorlevel 1 exit /b 1
"<run-root>\build-local\tooling\cmake\data\bin\cmake.exe" --build build-local\avalon-build --target llama-server -j 4
