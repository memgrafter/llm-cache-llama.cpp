#!/usr/bin/env python3
"""80k token prefill stress test: "TOK " * 80000 against stardart.local:8081"""

import time, json, requests

BASE = "http://stardart.local:8081/v1/chat/completions"
MODEL = "local-model"

prompt = "TOK " * 80000  # ~80k tokens

payload = {
    "model": MODEL,
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 256,         # let it generate a bit
    "stream": True,
}

print(f"Payload: {len(prompt):,} chars (~80k tokens)")
print("Sending request...")

t0 = time.time()
first_token_time = None
token_count = 0
reasoning_tokens = 0
content_tokens = 0
chunk_times = []
full_reasoning = ""
full_content = ""

with requests.post(BASE, json=payload, stream=True, timeout=600) as resp:
    print(f"Response status: {resp.status_code}")
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break

        chunk = json.loads(data_str)
        delta = chunk.get("choices", [{}])[0].get("delta", {})
        
        rc = delta.get("reasoning_content")
        ct = delta.get("content")
        
        if rc or ct:
            elapsed = time.time() - t0
            if first_token_time is None:
                first_token_time = elapsed
                print(f"\nFirst token at {first_token_time:.2f}s (TTFT)")
            
            token_count += 1
            chunk_times.append(elapsed)
            
            if rc:
                reasoning_tokens += 1
                full_reasoning += rc
                if reasoning_tokens <= 3 or reasoning_tokens % 50 == 0:
                    print(f"  [reasoning #{reasoning_tokens}] at {elapsed:.2f}s: {rc!r}")
            elif ct:
                content_tokens += 1
                full_content += ct
                if content_tokens <= 3 or content_tokens % 20 == 0:
                    print(f"  [content #{content_tokens}] at {elapsed:.2f}s: {ct!r}")

total_time = time.time() - t0

print(f"\n{'='*60}")
print(f"RESULTS:")
print(f"  Total time:          {total_time:.2f}s")
print(f"  Prefill (TTFT):      {first_token_time:.2f}s")
if first_token_time and total_time > first_token_time:
    gen_time = total_time - first_token_time
    gen_tok = token_count - 1
    print(f"  Gen time:            {gen_time:.2f}s")
    print(f"  Gen throughput:      {gen_tok / gen_time:.1f} tok/s")
print(f"  Total tokens out:    {token_count}")
print(f"  Reasoning tokens:    {reasoning_tokens}")
print(f"  Content tokens:      {content_tokens}")
if first_token_time:
    print(f"  Prefill throughput:  ~{80000 / first_token_time:.0f} tok/s (estimate)")
print(f"\nReasoning preview: {full_reasoning[:200]}...")
print(f"Content preview:   {full_content[:200]}...")
