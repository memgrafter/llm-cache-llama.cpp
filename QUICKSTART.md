# Quickstart — Why This Stack Exists

This repo is a local llama.cpp server with a KV-cache proxy in front of it, tuned for a 16 GB Mac Mini running Qwen3.6-28B.

**Note: This document was generated from data I curated. It was done with local `Qwen3.6-28B-REAP.i1-IQ3_XXS`. It's worth a scan.**

## The problem

A 28B parameter model needs ~56 GB of RAM at FP16 to load. On a 16 GB machine that's impossible without aggressive quantization. Even quantized, the KV cache (one per active slot) eats hundreds of megabytes each — and llama.cpp's built-in multi-prompt RAM cache tries to keep multiple prompts in memory simultaneously, which crashes the machine.

## What we do instead

**Disable llama.cpp's multi-prompt RAM cache.** `CACHE_RAM=0` turns it off entirely. Only slot 0 stays active. KV state gets saved/restored from disk instead of RAM. This means:

- One slot at a time (slot 0)
- KV state lives on disk after each request completes
- The proxy restores the best matching prefix from disk on the next request
- No more OOM crashes from competing prompts fighting for Metal memory

**Use an IQ3-quantized model.** [Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf](https://huggingface.co/mradermacher/Qwen3.6-28B-REAP-i1-GGUF?show_file_info=Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf) 

**Speculative decoding via ngram-mod.** No draft model needed — it uses a shared n-gram hash pool to draft tokens from repeated code/text patterns. llama.cpp still verifies all drafted tokens with the main model, so correctness isn't compromised. It's low-memory and works well for coding workloads where the same code blocks repeat across requests.

## Expected speed

Representative `logs/` runs on the 16 GB M4 Mac Mini with this IQ3 model are all in the same ballpark. Prefill here means new prompt tokens after restoring the closest disk checkpoint.

| Checkpoint context | Prefill | Generation |
| --- | ---: | ---: |
| ~3k tokens | ~84-92 t/s | ~28-29 t/s |
| ~5-7k tokens | ~72-82 t/s | ~26-28 t/s |
| ~13k tokens | ~55-57 t/s | ~22-23 t/s |
| ~19-22k tokens | ~37-46 t/s | ~19-21 t/s |
| ~24-30k tokens | ~24-36 t/s | ~16-18 t/s |
| ~32-33k tokens | ~26-28 t/s | ~15-16 t/s |

Best-case speculative generation with `ngram-mod` hit about **36-37 t/s** when the output had reusable repeated patterns. Treat that as an upside case; normal generation is closer to the table.

**Prefix trie on disk.** Instead of keeping prompts in RAM, the proxy hashes each prompt with BLAKE2b-128, stores the prefix node metadata in SQLite, and saves KV state as `.bin` files on disk. On the next request, it restores whatever prefix matches best from disk. Generated responses are also cached — rewinding a code session reuses previously generated tokens instead of regenerating them.



## What you lose (and why it's fine)

- No concurrent slots — only one request at a time. For a personal coding agent this is fine.
- Disk I/O on every request — but the prefix cache means the model rarely starts from scratch.
- No multi-prompt RAM caching — replaced by disk-based trie where pruning keeps things under control.

## Setup

### Download the model

Get `Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf` from:

https://huggingface.co/mradermacher/Qwen3.6-28B-REAP-i1-GGUF?show_file_info=Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf

Place it under the repo `models/` directory, or override with `MODEL=/path/to/model.gguf`.

### Build the TurboQuant llama.cpp binary

The IQ3 quantization kernels come from the TurboQuant build of llama.cpp:

```bash
git clone https://github.com/TheTom/llama-cpp-turboquant.git
cd llama-cpp-turboquant
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DLLAMA_METAL=ON
make -j$(sysctl -n hw.ncpu)
```

Update the path alias in `_llama-engine.sh` to point at your build:

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
