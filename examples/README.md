# Examples

Example scripts for the boto3 Infinia plugins (RDMA acceleration and unlimited tagging).

## Examples

### s3_rdma_example.py

```bash
python s3_rdma_example.py --bucket my-bucket [options]
```

| Option | Test scenario |
|---|---|
| *(none)* | Simple upload + download with RDMA (NumPy), with performance/throughput metrics |
| `--bucket BUCKET` | **(required)** S3 bucket name |
| `--endpoint-url URL` | Override S3 endpoint (e.g. MinIO); defaults to `AWS_ENDPOINT_URL_S3` env var or AWS S3 |
| `--size-mb N` | Size in MB for the simple upload/download test (default: 10) |
| `--multipart` | Multipart upload with RDMA (3 parts x 5MB), followed by download + verification |
| `--gpu` | GPU upload/download with CuPy — zero-copy GPU↔Network transfer (requires CuPy) |
| `--register-buffer` | Buffer pre-registration: register once, reuse across 5 upload+download cycles with `is_buffer_registered=True` |
| `--context-manager` | Automatic buffer registration/cleanup via `plugin.registered_buffer()` context manager, across 3 upload+download cycles |
| `--mixed-multipart` | Mixed multipart upload: part 1 bytearray (no RDMA), part 2 NumPy (RDMA), part 3 CuPy GPU (RDMA, if available); verifies full-object integrity on download |
| `--verbose`, `-v` | Verbose (DEBUG-level) logging |

Each scenario also verifies RDMA was actually used via response headers
(`x-amz-rdma-reply`, `x-amz-rdma-bytes-transferred`), confirms the HTTP response body is
empty, and falls back to a normal S3 call on `RDMANotSupportedError`.
