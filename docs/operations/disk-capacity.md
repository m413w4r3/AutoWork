# Disk capacity and Docker cleanup

`make disk-check` reports free space for Docker's root directory and the
`minio_data` volume mount when that volume exists. Thresholds use decimal GB
(1 GB = 1,000,000,000 bytes): `DISK_MIN_FREE_GB=25` is the hard floor and
`DISK_WARN_FREE_GB=50` is the warning threshold. Both values can be overridden
in the environment or on the Make command line.

`make up` and `make dev` run the check before Compose can build or start. Below
the hard floor, they stop and direct the operator to recover space. Between the
hard and warning thresholds, they prune aged BuildKit cache automatically;
`AUTO_GC=0` disables that automatic step. The cache target retains at most
`DOCKER_GC_KEEP_STORAGE_GB=15` GB and filters cache older than
`DOCKER_GC_UNTIL=168h`.

Use `make docker-gc DRY_RUN=1` to print the exact cleanup commands. Running
`make docker-gc` prunes only aged BuildKit cache within the configured storage
bound and dangling images (`docker image prune -f`). It never prunes volumes,
uses `system prune -a`, or removes tagged images. Volumes are never pruned
because `minio_data` holds canonical evidence blobs; deleting that volume can
destroy application data. The GC command does not remove images used by
containers or tagged images from this or other Compose projects.

## Recovery

If the hard floor blocks a build, inspect `make disk-check`, then run
`make docker-gc DRY_RUN=1` and review the proposed cache and dangling-image
cleanup. Run `make docker-gc`, check space again, and resume with `make up` or
`make dev` after the hard floor is met. If MinIO's volume is the constrained
path, freeing BuildKit cache may not free that separate filesystem; investigate
that storage's capacity and preserve the volume contents.

The Docker daemon also supports a persistent BuildKit garbage-collection policy.
This example is documentation only; it does not edit or reload the host daemon:

```json
{
  "builder": {
    "gc": {
      "enabled": true,
      "defaultKeepStorage": "15GB",
      "policy": [
        { "keepStorage": "15GB", "filter": ["until=168h"] },
        { "keepStorage": "5GB", "filter": ["unused-for=168h"] },
        { "keepStorage": "1GB" }
      ]
    }
  }
}
```

## Runtime capacity visibility

The backend readiness check currently confirms that the configured MinIO bucket
exists. The backend container does not mount the host Docker root or the
`minio_data` volume, and its regular object-storage client does not expose a
capacity probe without MinIO admin access. For that reason `/api/health/ready`
does not report a disk dependency, and no production-stage capacity gate is
installed. A future guard needs an authorized host/MinIO capacity signal and
must run at production-stage entry before it calls
`ModelConversationService.add_turn` (which archives the input blob) and before
direct calls reach `ModelGateway._execute` and commit a submission claim. These
paths persist different idempotency state, so one gateway preflight cannot
safely cover both. The existing `ModelGateway.preflight` is too late: the run
claim has already been committed. A failure in
`ModelGateway._complete_run` is later still; the provider may have completed,
so that case must retain reconciliation and recover the exact response rather
than resubmit.

An observed `XMinioStorageFull` on a blob write is classified as
`storage_capacity_low`. A write failure before model submission is retryable.
If output archival fails after a provider may have completed, the run remains
in reconciliation; retrying the same model call could lose or duplicate a
completed answer.
