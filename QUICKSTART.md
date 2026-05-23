# Quickstart — Why This Stack Exists

This repo is a local llama.cpp server with a KV-cache proxy in front of it, tuned for a 16 GB Mac Mini running Qwen3.6-28B.

## The problem

A 28B parameter model needs ~56 GB of RAM at FP16 to load. On a 16 GB machine that's impossible without aggressive quantization. Even quantized, the KV cache (one per active slot) eats hundreds of megabytes each — and llama.cpp's built-in multi-prompt RAM cache tries to keep multiple prompts in memory simultaneously, which crashes the machine.

## What we do instead

**Disable llama.cpp's multi-prompt RAM cache.** `CACHE_RAM=0` turns it off entirely. Only slot 0 stays active. KV state gets saved/restored from disk instead of RAM. This means:

- One slot at a time (slot 0)
- KV state lives on disk after each request completes
- The proxy restores the best matching prefix from disk on the next request
- No more OOM crashes from competing prompts fighting for Metal memory

**Use an IQ3-quantized model.** [Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf](https://huggingface.co/mradermacher/Qwen3.6-28B-REAP-i1-GGUF?show_file_info=Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf) is an intermediate-quant variant from the TurboQuant build path — smaller than full FP16 but still retains enough quality for coding/reasoning workloads. The TurboQuant build (`llama-cpp-turboquant-build-metal`) includes IQ3 kernel support that makes this quantization actually usable on Metal.

**Speculative decoding via ngram-mod.** No draft model needed — it uses a shared n-gram hash pool to draft tokens from repeated code/text patterns. llama.cpp still verifies all drafted tokens with the main model, so correctness isn't compromised. It's low-memory and works well for coding workloads where the same code blocks repeat across requests.

**Prefix trie on disk.** Instead of keeping prompts in RAM, the proxy hashes each prompt with BLAKE2b-128, stores the prefix node metadata in SQLite, and saves KV state as `.bin` files on disk. On the next request, it restores whatever prefix matches best from disk. Generated responses are also cached — rewinding a code session reuses previously generated tokens instead of regenerating them.



## What you lose (and why it's fine)

- No concurrent slots — only one request at a time. For a personal coding agent this is fine.
- Disk I/O on every request — but the prefix cache means the model rarely starts from scratch.
- No multi-prompt RAM caching — replaced by disk-based trie where pruning keeps things under control.

## Setup

### Download the model

Get `Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf` from:

https://huggingface.co/mradermacher/Qwen3.6-28B-REAP-i1-GGUF?show_file_info=Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf

Place it at the default location or override with `MODEL=/path/to/model.gguf`.

### Build the TurboQuant llama.cpp binary

The IQ3 quantization kernels come from the TurboQuant build of llama.cpp:

```bash
git clone https://github.com/TheTom/llama-cpp-turboquant.git
cd llama-cpp-turboquant
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DLLAMA_METAL=ON
make -j$(sysctl -n hw.ncpu)
```

Update the path alias in `run-qwen36-reap.sh` to point at your build:

```bash
export LOCAL_TURBO_BUILD="$(pwd)"  # or set it directly in the script
```

The script will autodetect `llama-server` from `builds/llama-cpp-turboquant-build-metal` by default.

### Raise Metal wired-memory limit

```bash
sudo sysctl iogpu.wired_limit_mb=14336
```

### Start the stack

```bash
./run-lmcache-proxy-stack.sh
```

### Configure `~/.pi/agent/models.json`

Configure pi to point at `http://127.0.0.1:8081/v1` with model `qwen3.6-28b-reap-iq3xxs-turbo3-35k`.
