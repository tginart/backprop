# Bucket backend

Use a GCS or S3 prefix as a shared Backprop installation. Training jobs write
directly to the bucket; each person who wants the dashboard runs a local viewer.
See the [bucket backend README](../backend/bucket/README.md) for the architecture.

## 1. Install

```bash
python -m pip install 'backprop-foundry[bucket]'        # GCS
python -m pip install 'backprop-foundry[bucket,aws]'    # S3
```

## 2. Grant access

Writers and viewers create and read objects; nothing is overwritten or deleted.

```bash
gcloud storage buckets add-iam-policy-binding gs://your-bucket \
    --member=serviceAccount:trainer@project.iam.gserviceaccount.com \
    --role=roles/storage.objectCreator
gcloud storage buckets add-iam-policy-binding gs://your-bucket \
    --member=serviceAccount:trainer@project.iam.gserviceaccount.com \
    --role=roles/storage.objectViewer
```

On a workstation, `gcloud auth application-default login` provides credentials.
For S3, grant `s3:PutObject`, `s3:GetObject` and `s3:ListBucket` on the prefix.

## 3. Log from training jobs

```bash
export BACKPROP_BACKEND=bucket
export BACKPROP_BUCKET_URI=gs://your-bucket/backprop
python train.py
```

`BACKPROP_CONFIG` can instead point at a file that contains
`{"backend": "bucket", "uri": "gs://your-bucket/backprop"}`. The SDK journal,
retries, offline mode, `backprop sync`, artifacts, resume, and cooperative stops
work as on the other backends.

## 4. View

```bash
backprop serve gs://your-bucket/backprop
```

The first start downloads every segment into
`~/.cache/backprop/bucket/<hash>/`. Later starts download only new objects.

| Option | Default | Use |
| --- | --- | --- |
| `--refresh-s` | 15 | Seconds between bucket refreshes. |
| `--cache-dir` | `~/.cache/backprop/bucket/<hash>` | Mirror and object cache location. One `serve` per directory. |
| `--port` | 8765 | Loopback dashboard port. |

## Costs and limits

- Each SDK flush (every 5 s while a run logs) writes one object per run. An
  idle run writes one per 30 s heartbeat.
- Each viewer refresh makes about 1 + projects + active runs list calls, plus a
  full listing of every run every five minutes.
- Objects are never deleted. Compaction of small segments is future work.
- The S3 adapter is not tested.
