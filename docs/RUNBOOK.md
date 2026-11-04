# Runbook - local-llm-k8s

Everything here runs on one Mac: one Lima VM, one single-node k3s, one model.

> Short version for a restart: [RESTART_CHEATSHEET.html](RESTART_CHEATSHEET.html).

## Start and stop

```bash
make up        # create/start the VM, install k3s, build + deploy, wait for Ready
make demo      # one question through the gateway (the proof)
make bench     # benchmark grid -> bench/results.{json,csv} + charts
make cost      # break-even table + chart from the measured results
make down      # delete the workloads, keep the VM
make stop      # shut the VM down (state and model cache survive)
make destroy   # delete the VM (asks first)
```

`make up` is idempotent. Re-running it after a reboot starts the VM, waits for
k3s, rebuilds the gateway image only if the manifest changed, and re-applies.

## Is it healthy?

```bash
kubectl get nodes                      # expect: Ready
kubectl -n llm get pods,svc,hpa
kubectl -n llm rollout status deployment/ollama
curl -s http://127.0.0.1:30080/healthz # liveness: the gateway process
curl -s http://127.0.0.1:30080/readyz  # readiness: Ollama + model present
```

The gateway answers `/readyz` with 503 and a reason (`ollama_unreachable` or
`model_missing`) until the model is in memory. That is by design: the readiness
probe is what stops traffic reaching a pod that cannot serve.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `kubectl` times out on 127.0.0.1:6443 | Lima VM stopped or port forward died | `make stop && make up` |
| gateway pods `ImagePullBackOff` | image not imported into k3s containerd | `./scripts/build_gateway.sh` |
| gateway `CrashLoopBackOff`, `Permission denied: /app/app.py` | build context file mode leaked into the image | rebuild; the Dockerfile uses `COPY --chmod=644` |
| `/readyz` 503 `model_missing` | model pull still running (first boot) | `kubectl -n llm logs deploy/ollama -c pull-model` |
| Ollama OOMKilled | model too large for the 3 Gi memory limit | use `qwen2.5:0.5b-instruct`; 1.5B needs a bigger limit |
| HPA shows `<unknown>` CPU | metrics-server not ready yet | wait 60 s; k3s runs metrics-server by default |

## Logs

```bash
kubectl -n llm logs -l app.kubernetes.io/name=gateway -f   # one JSON line per /ask
kubectl -n llm logs -l app.kubernetes.io/name=ollama -f
kubectl -n llm describe pod -l app.kubernetes.io/name=ollama
```

Every `/ask` writes one JSON log line with `request_id`, `latency_ms`,
`prompt_tokens`, `completion_tokens`, `tokens_per_second` and `status`.

## Scaling

The gateway is stateless and scales horizontally; Ollama is one workload and
does not. The HPA targets gateway CPU at 50% average utilisation, min 1, max 4.
To pin replicas for a fixed-replica benchmark, patch the HPA to `min=max=N`
first, otherwise the HPA fights `kubectl scale` (this is what `bench/run_bench.py`
does automatically).

## Rebuilding after a code change

```bash
./scripts/build_gateway.sh
kubectl -n llm rollout restart deployment/gateway
kubectl -n llm rollout status deployment/gateway
```

## Ground rules this cluster follows

- No cloud spend: the cluster runs in a local VM.
- One model, one node, no GPU. Throughput numbers are CPU-only and do not
  transfer to a GPU cluster.
- Model weights live on a `local-path` PVC so a pod restart does not re-download.
