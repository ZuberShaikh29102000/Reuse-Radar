# Bulk extraction on a rented GPU

The free tiers extract about 20 papers a day. For the full corpus, the same open-weights model
Groq serves (`openai/gpt-oss-120b`) runs on a rented GPU under vLLM, and the pipeline talks to it
as one more OpenAI-compatible provider (`OpenAICompatibleProvider` in `reuse_radar/llm/router.py`).

## Shape of the run

```
laptop (pipeline, caches, data/)  --SSH tunnel-->  GPU machine (vLLM serving gpt-oss-120b)
        localhost:8001                                      localhost:8000
```

- The model server listens only on the GPU machine's localhost. The laptop reaches it through an
  SSH tunnel, so nothing is exposed to the internet and no data leaves the two machines.
- The pipeline, its caches and `data/` stay on the laptop, as for every other run. Only prompts
  (filtered paper passages) and answers cross the tunnel.

## Steps

1. Create the GPU machine. Used on 2026-10-31: AMD Developer Cloud, 1x MI300X (192 GB), image
   "ROCm Software", an SSH key made with `ssh-keygen -t ed25519 -f ~/.ssh/amd_gpu`.
2. On the GPU machine (`ssh -i ~/.ssh/amd_gpu root@<public-ip>`), start vLLM serving
   `openai/gpt-oss-120b` on port 8000, bound to localhost only:

   ```sh
   docker run -d --name vllm --network=host --device=/dev/kfd --device=/dev/dri \
     --group-add=video --ipc=host --security-opt seccomp=unconfined --shm-size 32G \
     -v /root/hf:/root/.cache/huggingface --entrypoint vllm \
     vllm/vllm-openai-rocm:latest serve openai/gpt-oss-120b --host 127.0.0.1 --port 8000
   docker logs -f vllm   # wait for "Application startup complete" (about 20-40 min)
   ```

3. On the laptop, open the tunnel (keep this terminal open):

   ```sh
   ssh -i ~/.ssh/amd_gpu -N -L 8001:localhost:8000 root@<public-ip>
   ```

4. Check the server answers: `curl http://localhost:8001/v1/models`.
5. Point the pipeline at it, for this terminal only, and extract with parallel workers:

   ```powershell
   $env:LOCAL_LLM_BASE_URL = "http://localhost:8001/v1"
   uv run python -m reuse_radar.pipeline.extract --workers 32
   uv run python -m reuse_radar.pipeline.run --stages reconcile store
   Remove-Item Env:LOCAL_LLM_BASE_URL
   ```

6. Delete the GPU machine as soon as extraction finishes (it bills while it exists), then
   upload to the production database as after any run (`docs/deploy.md`).

## Behaviour

- With `LOCAL_LLM_BASE_URL` set, the GPU server is the only provider: if it fails, the run stops
  instead of falling back to the free tiers and spending their quota.
- Papers already extracted at the current `EXTRACTION_VERSION` are kept, so the run picks up
  exactly the papers still missing, and can be stopped and restarted at any time.
- Answers are cached under the provider name `vllm`, separate from Groq's entries for the same
  model, so the two sources stay distinguishable.
- `--workers` sets how many papers are extracted at once. vLLM batches concurrent requests, so
  throughput grows with workers until the GPU is saturated.
- The server gets at least 12,000 output tokens per request (`min_output_tokens`), not the 3,000
  sized for Groq's per-minute quota: reasoning counts against the cap, and at 3,000 about one
  paper in six was cut off.

## Result of the 2026-10-31 run

- Quality on the gold set, same prompt: precision 0.91, recall 0.68, F1 0.78 (Groq: 0.91, 0.65,
  0.76).
- 385 papers in about 50 minutes at 32 workers (a first pass at the old 3,000 cap, then the 79
  cut-off papers again at 12,000), about 3.1M input and 2.4M output tokens.
- About 1.25 GPU hours at $1.99/hour, versus about 20 days on the free tiers.
