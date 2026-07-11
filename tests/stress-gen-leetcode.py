#!/usr/bin/env python3
"""Generation throughput stress test with LeetCode-style problems."""

import time, json, requests

BASE = "http://stardart.local:8081/v1/chat/completions"
MODEL = "local-model"

PROBLEMS = [
    {
        "name": "LRU Cache Implementation",
        "prompt": (
            "Implement an LRU (Least Recently Used) cache in Python.\n\n"
            "The cache should support:\n"
            "- `get(key)` → returns value or -1 if not found, O(1)\n"
            "- `put(key, value)` → inserts/updates, evicts least recently used if full, O(1)\n\n"
            "Write the complete class with a doubly-linked list + hash map.\n"
            "Include detailed comments explaining each operation.\n"
            "Then provide 3 test cases demonstrating get, put, and eviction behavior.\n"
            "Finally, analyze time and space complexity."
        ),
    },
    {
        "name": "Merge K Sorted Lists",
        "prompt": (
            "Given k sorted linked lists, merge them into one sorted list.\n\n"
            "Input: lists = [[1,4,5],[1,3,4],[2,6]]\n"
            "Output: [1,1,2,3,4,4,5,6]\n\n"
            "Implement this in Python using a min-heap (priority queue).\n"
            "Write the full solution with:\n"
            "- A ListNode class definition\n"
            "- The mergeKLists function\n"
            "- Helper functions to build and print lists\n"
            "- 4 test cases of increasing difficulty\n"
            "- Detailed explanation of why a heap is optimal here\n"
            "- Time and space complexity analysis\n"
            "- Comparison with the divide-and-conquer approach"
        ),
    },
    {
        "name": "Trapping Rain Water",
        "prompt": (
            "Given n non-negative integers representing an elevation map where each bar has width 1,\n"
            "compute how much water it can trap after raining.\n\n"
            "Input: height = [0,2,0,3,1,0,1,3,2,1]\n"
            "Output: 9\n\n"
            "Implement the optimal two-pointer solution in Python.\n"
            "Include:\n"
            "- The full function with inline comments explaining each step\n"
            "- A visual diagram showing how water is trapped for the example input\n"
            "- 5 test cases including edge cases (empty, monotonic, single peak)\n"
            "- Step-by-step trace of the algorithm on the main example\n"
            "- Why this approach is better than the brute-force O(n²) method\n"
            "- Time and space complexity analysis"
        ),
    },
    {
        "name": "Sliding Window Maximum",
        "prompt": (
            "Given an array nums of size n, find the maximum value in every sliding window of size k.\n\n"
            "Input: nums = [1,3,-1,-3,5,3,6,7], k = 3\n"
            "Output: [3,3,5,5,6,7]\n\n"
            "Implement the O(n) solution using a deque (monotonic queue) in Python.\n"
            "Include:\n"
            "- The full function with detailed comments\n"
            "- Explanation of why a monotonic deque works\n"
            "- Step-by-step trace showing the deque state at each index\n"
            "- 4 test cases including edge cases (k=1, k=n, all same values)\n"
            "- Comparison with the brute-force O(nk) approach\n"
            "- Time and space complexity analysis"
        ),
    },
    {
        "name": "Median of Two Sorted Arrays",
        "prompt": (
            "Given two sorted arrays nums1 and nums2 of size m and n respectively,\n"
            "return the median of the two sorted arrays.\n\n"
            "Input: nums1 = [1,3], nums2 = [2]\n"
            "Output: 2.0\n\n"
            "Implement the O(log(min(m,n))) binary search solution in Python.\n"
            "Include:\n"
            "- The full function with extensive inline comments\n"
            "- Detailed explanation of the partition approach\n"
            "- Step-by-step trace on two different examples showing how partitions shift\n"
            "- 6 test cases covering: odd+odd, even+even, odd+even lengths, one empty array\n"
            "- Why binary search on the smaller array is optimal\n"
            "- Time and space complexity analysis\n"
            "- What happens at each comparison and why we eliminate left or right half"
        ),
    },
]

def run_test(problem):
    """Run a single LeetCode problem and measure generation throughput."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": problem["prompt"]}],
        "max_tokens": 4096,
        "stream": True,
    }

    t0 = time.time()
    first_token_time = None
    token_count = 0
    reasoning_tokens = 0
    content_tokens = 0
    full_text = ""

    with requests.post(BASE, json=payload, stream=True, timeout=600) as resp:
        if resp.status_code != 200:
            print(f"  ERROR: HTTP {resp.status_code}")
            return None

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
                
                token_count += 1
                if rc:
                    reasoning_tokens += 1
                    full_text += f"[think]{rc}[/think]"
                elif ct:
                    content_tokens += 1
                    full_text += ct

    total_time = time.time() - t0
    
    results = {
        "name": problem["name"],
        "total_time": total_time,
        "ttft": first_token_time,
        "total_tokens": token_count,
        "reasoning_tokens": reasoning_tokens,
        "content_tokens": content_tokens,
    }
    
    if first_token_time and token_count > 1:
        gen_time = total_time - first_token_time
        results["gen_throughput"] = (token_count - 1) / gen_time
    else:
        results["gen_throughput"] = 0
    
    return results

def main():
    print("=" * 70)
    print("LeetCode Generation Throughput Stress Test")
    print("=" * 70)
    
    all_results = []
    
    for i, problem in enumerate(PROBLEMS, 1):
        print(f"\n--- Problem {i}/{len(PROBLEMS)}: {problem['name']} ---")
        result = run_test(problem)
        if result:
            all_results.append(result)
            print(f"  TTFT:          {result['ttft']:.2f}s")
            print(f"  Total time:    {result['total_time']:.2f}s")
            print(f"  Tokens out:    {result['total_tokens']} (R:{result['reasoning_tokens']} C:{result['content_tokens']})")
            print(f"  Gen TPS:       {result['gen_throughput']:.1f} tok/s")
    
    # Summary table
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"{'Problem':<35} {'Tokens':>6} {'Gen TPS':>8} {'TTFT':>6}")
    print("-" * 70)
    for r in all_results:
        print(f"{r['name']:<35} {r['total_tokens']:>6} {r['gen_throughput']:>7.1f} {r['ttft']:>5.1f}")
    
    if all_results:
        avg_tps = sum(r['gen_throughput'] for r in all_results) / len(all_results)
        total_tok = sum(r['total_tokens'] for r in all_results)
        print("-" * 70)
        print(f"{'Average':<35} {total_tok:>6} {avg_tps:>7.1f}")

if __name__ == "__main__":
    main()
