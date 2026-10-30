# Dynamic request B001 — candidate #3

Sample SHA-256: `3fd810a3aa0039993393741b32287c367a9a5037a41e826906440887cdd3ed13`
Canonical sample association: `e0a2fd48-42d3-526e-8fd0-96072f460891`

## Objective
In one bounded run, determine whether the managed `PsProxy.PSExecutor.Execute` path reaches concrete cryptographic, decoding, network, or process-creation APIs. The untrusted article hypothesis to assess is claim ID `S3P2.1`; treat it as a lead only, not ground truth.

## Observe
- Managed method and call sequence, including argument/return lengths and stack context around the inspected Execute path.
- Crypto and decode APIs actually invoked; record algorithm/mode and key/IV lengths, plus hashes of input/output buffers. Keep any raw key bytes inside protected dynamic result artifacts only; exclude them from summaries and logs.
- HTTP, DNS, socket, and process-creation API attempts, including whether each attempt was blocked; capture generated destination metadata only as needed to describe the attempted behavior.
- Whether a network-independent configuration or payload is decoded, and its artifact hash/type.

## Isolation boundary
Use the authorized isolated QEMU/KVM + Frida setup. No external network route, DNS resolution, live C2 contact, or successful outbound connection. Intercept and block all network attempts before transmission; no host mounts, persistence, or repeated execution loops. Do not execute on the operator host.

Return a concise, timestamped call trace with attempted versus completed operations clearly separated, tool/runtime versions, environmental limitations, and hashes of any retained result artifacts under `dynamic_results/`.
