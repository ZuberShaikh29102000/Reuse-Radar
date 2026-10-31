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

1. On the GPU machine, start vLLM serving `openai/gpt-oss-120b` on port 8000.
2. On the laptop, open the tunnel (keep this terminal open):

   ```sh
   ssh -N -L 8001:localhost:8000 <user>@<gpu-machine-address>
   ```

3. Check the server answers: `curl http://localhost:8001/v1/models`.
4. Point the pipeline at it, for this terminal only, and extract with parallel workers:

   ```powershell
   $env:LOCAL_LLM_BASE_URL = "http://localhost:8001/v1"
   uv run python -m reuse_radar.pipeline.extract --workers 16
   uv run python -m reuse_radar.pipeline.run --stages reconcile store
   Remove-Item Env:LOCAL_LLM_BASE_URL
   ```

5. Upload to the production database as after any run (`docs/deploy.md`).

## Behaviour

- With `LOCAL_LLM_BASE_URL` set, the GPU server is the only provider: if it fails, the run stops
  instead of falling back to the free tiers and spending their quota.
- Papers already extracted at the current `EXTRACTION_VERSION` are kept, so the run picks up
  exactly the papers still missing, and can be stopped and restarted at any time.
- Answers are cached under the provider name `vllm`, separate from Groq's entries for the same
  model, so the two sources stay distinguishable.
- `--workers` sets how many papers are extracted at once. vLLM batches concurrent requests, so
  throughput grows with workers until the GPU is saturated.
