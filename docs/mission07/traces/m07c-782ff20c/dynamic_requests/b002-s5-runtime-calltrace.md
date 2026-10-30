# Dynamic request B002 — candidate #5

Sample SHA-256: `53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402`
Canonical sample association: `10b3faaa-b77b-54bd-891d-1c7f6234e3f9`

## Objective
In one bounded run, determine whether the obfuscated managed candidate decodes a configuration and actually invokes crypto, network, or process-creation APIs. The untrusted article hypothesis to assess is claim ID `S4P1.1`; treat it as a lead only, not ground truth.

## Observe
- Runtime unpacking/deobfuscation and managed method transitions, with call sequence and buffer lengths/hashes.
- Crypto/decode APIs actually invoked; record algorithm/mode, key/IV lengths and hashes of inputs/outputs. Keep raw key bytes only in protected dynamic result artifacts, never in summaries or logs.
- HTTP, DNS, socket, and process-creation API attempts, including blocked status and stack context. Distinguish a type reference or attempted call from a completed process/network action.
- Any decoded configuration or payload artifact type/hash, without launching child payloads.

## Isolation boundary
Use the authorized isolated QEMU/KVM + Frida setup. No external network route, DNS resolution, live C2 contact, or successful outbound connection. Intercept and block all network attempts before transmission; no host mounts, persistence, or repeated execution loops. Do not execute on the operator host.

Return a concise, timestamped call trace with attempted versus completed operations clearly separated, tool/runtime versions, environmental limitations, and hashes of any retained result artifacts under `dynamic_results/`.
