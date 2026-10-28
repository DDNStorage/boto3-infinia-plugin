# Boto3 Infinia Plugin

Non-intrusive boto3 extensions for [DDN Infinia](https://www.ddn.com/products/infinia/) features that go beyond the standard S3 API.

## Overview

Infinia extends the S3 API with capabilities that stock boto3 cannot express. This project provides
small, opt-in plugins that add those capabilities to a regular boto3 S3 client without modifying
boto3, botocore, s3transfer, aws-crt-python, or aws-c-s3. Each plugin is independent and they can be
used together on the same client.

| Feature | Package | Summary |
|---|---|---|
| [RDMA acceleration](#rdma-acceleration) | `boto3_infinia.rdma` | Move object data over RDMA (including directly to/from GPU memory) instead of the HTTP body. |
| [Unlimited tagging](#unlimited-tagging) | `boto3_infinia.tagging` | Read and delete object/bucket tags beyond AWS's 10-tag limit, with paging and prefix filtering. |

Both plugins are installed from the same distribution; see [Build & Installation](#build--installation).

## API Reference

See [API_REFERENCE.md](API_REFERENCE.md) for the full `Boto3RdmaPlugin` class and method reference.
The tagging plugin is documented in [TAGGING_PLUGIN_README.md](TAGGING_PLUGIN_README.md).

---

## Build & Installation

This project is not published on PyPI, so it must be built from source and installed locally
with `uv`. The single `boto3-infinia-plugin` distribution provides both plugins
(`boto3_infinia.rdma` and `boto3_infinia.tagging`).

```bash
# Build the wheel
uv build

# Install the built wheel into your project/environment
pip install dist/boto3_infinia_plugin-*.whl
```

The tagging plugin has no extra requirements. The RDMA plugin additionally needs the CUDA
toolkit/driver and cuobjclient; see [RDMA prerequisites](#rdma-prerequisites).

**Working on this repo / running the examples:** instead of building a wheel, use
`uv sync` to install directly into a local virtualenv (see
[examples/README.md](examples/README.md)).

---

## RDMA Acceleration

A non-intrusive RDMA acceleration layer for boto3 S3 operations.

This plugin adds RDMA (Remote Direct Memory Access) support to boto3 S3 operations. It uses
monkey-patching and boto3's event system to intercept requests and inject RDMA tokens.

Note that we make use of event registration to inject RDMA tokens, so it doesn't support boto3 with CRT, because CRT doesn't use event system.
It's not a big problem because boto3 doesn't use CRT by default, and the default boto3 is the most widely used.

Also, we hook the low-level APIs (put_object, get_object, upload_part), so it doesn't support high-level APIs like `download_file`, `upload_file`, etc.

Please refer to https://docs.aws.amazon.com/boto3/latest/reference/services/s3.html for all low-level APIs of boto3.

### Features

- ✅ Non-intrusive: No modifications to upstream packages
- ✅ Simple API: Just add `buffer=` parameter to S3 calls
- ✅ Automatic fallback: Gracefully falls back to normal S3 on error
- ✅ GPU memory support: Works with NumPy, CuPy, and any buffer protocol
- ✅ Multipart upload support: RDMA acceleration for large files

### How It Works

1. **Monkey-patching**: Wraps `get_object`, `put_object`, and `upload_part` to accept `buffer` parameter
2. **Context Variables**: Passes buffer information from wrapper to event handlers
3. **Event Hooks**: Uses `before-sign` to inject `x-amz-rdma-token` header (included in signature)
4. **Server Protocol**: Server reads RDMA token and transfers data directly to/from buffer
5. **Cleanup**: Uses `after-call` and `after-call-error` to free RDMA resources

### Server Protocol

The wire protocol (token format, negotiation, headers, and server implementation
requirements) is specified in NVIDIA's
[RDMA protocol specification](https://github.com/KiranModukuri/aws-c-s3/blob/nvidia_rdma/RDMA_PROTOCOL_SPEC.md).

### Limitations

- **No checksum support for RDMA-enabled requests yet:** the plugin does not compute a
  checksum over the buffer data for RDMA `PutObject`/`UploadPart` calls. Until this is fixed,
  checksum calculation and validation must be disabled via `AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED`
  and `AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED` (set in
  [`env_rdma.sh.example`](env_rdma.sh.example)); otherwise botocore forces `aws-chunked`
  trailer-checksum encoding, which is incompatible with RDMA's empty-body requests.
- **Unreliable on ARM:** the plugin depends on `cppyy`, which has no pre-built binary
  packages for ARM. `pip install cppyy` will fall back to building it from source on ARM,
  but this build has many dependencies and is heavily environment-dependent, so it often
  fails. A future version will replace `cppyy` with traditional C wrappers (built at plugin
  build time) to make ARM support reliable.

### RDMA Prerequisites

Install the latest CUDA toolkit, CUDA driver (for GPU) and `libcuobjclient-dev` first.

cuobjclient is discovered via pkg-config (package name format `cuobjclient-<version>`); set
`PKG_CONFIG_PATH` if it's installed in a non-standard location:

```bash
export PKG_CONFIG_PATH=/path/to/cuobjclient/lib/pkgconfig:$PKG_CONFIG_PATH
pkg-config --exists cuobjclient-1.3.0 && echo "Found" || echo "Not found"
```

### Configuration

Copy the provided example script, edit it with your cuFile config path, S3 endpoint, and
credentials, then source it:

```bash
cp env_rdma.sh.example env_rdma.sh
# Edit env_rdma.sh ...
source env_rdma.sh
```

See [`env_rdma.sh.example`](env_rdma.sh.example) for all available variables:
`CUFILE_ENV_PATH_JSON` (path to the cuObjClient JSON config, defaults to `/etc/cufile.json`),
`AWS_ENDPOINT_URL_S3`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, and the
checksum-calculation variables required to disable `aws-chunked`/trailer-checksum encoding for
RDMA uploads.

**Example cufile.json:**
```json
{
  "properties": {
    "rdma_dev_addr_list": ["<rdma-device-ip>"]
  }
}
```

**Note:** Configure `rdma_dev_addr_list` with your RDMA device IP address.

Endpoint and credentials can also be set via boto3's standard AWS config/credentials files
(`~/.aws/config`, `~/.aws/credentials`) or passed directly to `boto3.client('s3', ...)`
— the plugin doesn't change boto3's normal configuration resolution.

### Quick Start

```python
import boto3
import numpy as np
from boto3_infinia.rdma import Boto3RdmaPlugin

# Initialize S3 client and plugin
# Note: Endpoint URL can be configured via AWS_ENDPOINT_URL_S3 env var
s3 = boto3.client('s3')
plugin = Boto3RdmaPlugin(s3)

# Download with RDMA (automatic buffer registration)
buffer = np.zeros(10 * 1024 * 1024, dtype=np.uint8)
response = s3.get_object(Bucket='my-bucket', Key='my-key', buffer=buffer)

# Upload with RDMA (automatic buffer registration)
data = np.random.randint(0, 256, size=10*1024*1024, dtype=np.uint8)
response = s3.put_object(Bucket='my-bucket', Key='my-key', buffer=data)

# Performance optimization: pre-register for multiple operations
# Option 1: Manual registration (requires explicit cleanup)
plugin.register_buffer(buffer)  # Register once
for i in range(100):
    response = s3.get_object(
        Bucket='my-bucket',
        Key=f'my-key-{i}',
        buffer=buffer,
        is_buffer_registered=True  # Skip re-registration
    )
plugin.unregister_buffer(buffer)  # Cleanup when done

# Option 2: Context manager (automatic cleanup, recommended)
with plugin.registered_buffer(buffer):
    for i in range(100):
        response = s3.get_object(
            Bucket='my-bucket',
            Key=f'my-key-{i}',
            buffer=buffer,
            is_buffer_registered=True
        )
# Buffer is automatically unregistered here
```

### Usage Examples

#### Basic Upload/Download

```python
import boto3
import numpy as np
from boto3_infinia.rdma import Boto3RdmaPlugin

s3 = boto3.client('s3')
plugin = Boto3RdmaPlugin(s3)

# Upload
data = np.random.bytes(1024 * 1024)  # 1MB
s3.put_object(Bucket='bucket', Key='file.bin', buffer=data)

# Download
buffer = np.zeros(1024 * 1024, dtype=np.uint8)
s3.get_object(Bucket='bucket', Key='file.bin', buffer=buffer)
```

#### Multipart Upload

```python
# Create multipart upload
mpu = s3.create_multipart_upload(Bucket='bucket', Key='large-file.bin')
upload_id = mpu['UploadId']

# Upload parts with RDMA
parts = []
for i in range(1, 4):
    part_data = np.random.randint(0, 256, size=5*1024*1024, dtype=np.uint8)
    response = s3.upload_part(
        Bucket='bucket',
        Key='large-file.bin',
        UploadId=upload_id,
        PartNumber=i,
        buffer=part_data
    )
    parts.append({'PartNumber': i, 'ETag': response['ETag']})

# Complete upload
s3.complete_multipart_upload(
    Bucket='bucket',
    Key='large-file.bin',
    UploadId=upload_id,
    MultipartUpload={'Parts': parts}
)
```

#### Error Handling and Fallback

```python
from boto3_infinia.rdma import RDMANotSupportedError

try:
    s3.get_object(Bucket='bucket', Key='file.bin', buffer=buffer)
except RDMANotSupportedError:
    # Fallback to normal S3 download
    response = s3.get_object(Bucket='bucket', Key='file.bin')
    buffer[:] = np.frombuffer(response['Body'].read(), dtype=np.uint8)
```

#### GPU Memory (CuPy)

```python
import cupy as cp

# Download directly to GPU
gpu_buffer = cp.zeros(10 * 1024 * 1024, dtype=cp.uint8)
s3.get_object(Bucket='bucket', Key='file.bin', buffer=gpu_buffer)

# Upload from GPU
gpu_data = cp.random.randint(0, 256, size=10*1024*1024, dtype=cp.uint8)
s3.put_object(Bucket='bucket', Key='file.bin', buffer=gpu_data)
```

#### Pre-registered Buffers (Performance Optimization)

For buffers used multiple times, you can pre-register them to skip registration overhead on each operation:

##### Option 1: Manual Registration

```python
import numpy as np

# Create buffer
buffer = np.zeros(10 * 1024 * 1024, dtype=np.uint8)

# Pre-register buffer using the plugin's helper method
plugin.register_buffer(buffer)

# Use with is_buffer_registered=True to skip registration
for i in range(100):
    s3.get_object(
        Bucket='bucket',
        Key=f'file{i}.bin',
        buffer=buffer,
        is_buffer_registered=True  # Skip registration, improves performance
    )

# When done, clean up using the plugin's helper method
plugin.unregister_buffer(buffer)
```

##### Option 2: Context Manager (Recommended)

```python
import numpy as np

# Create buffer
buffer = np.zeros(10 * 1024 * 1024, dtype=np.uint8)

# Use context manager for automatic cleanup
with plugin.registered_buffer(buffer):
    for i in range(100):
        s3.get_object(
            Bucket='bucket',
            Key=f'file{i}.bin',
            buffer=buffer,
            is_buffer_registered=True
        )
# Buffer is automatically unregistered here, even if exception occurred
```

**Benefits:**
- ✅ Automatic buffer type detection (NumPy, CuPy)
- ✅ Automatic pointer conversion (GPU/CPU)
- ✅ Skips redundant buffer registration on every operation
- ✅ Reduces RDMA setup overhead for repeated operations
- ✅ Context manager guarantees cleanup even on exceptions
- ✅ Simple API - no manual size calculation or pointer management

**Supported Buffer Types:**
- NumPy arrays (`numpy.ndarray`)
- CuPy arrays (`cupy.ndarray`)
- Any object supporting Python buffer protocol

**Important:** Always call `plugin.unregister_buffer()` or use the context manager when done to free RDMA resources.

### Running Examples

The `examples/` directory contains comprehensive demonstrations:

```bash
uv sync --extra examples --extra gpu

# Copy the example env script, edit it with your endpoint/credentials/cuFile config, then source it
cp env_rdma.sh.example env_rdma.sh
# edit env_rdma.sh ...
source env_rdma.sh

uv run examples/s3_rdma_example.py --bucket <pre-created-bucket>
```

See [examples/README.md](examples/README.md) for the full command-line options and
corresponding test scenarios.

### Troubleshooting

#### Plugin initialization fails
```
Error: cuobjclient-* package not found via pkg-config
```
**Solution:** Install cuobjclient and set `PKG_CONFIG_PATH` (see [RDMA Prerequisites](#rdma-prerequisites)).

#### RDMA not supported error
```
RDMANotSupportedError: RDMA not supported by server
```
**Solution:** Server doesn't support RDMA or returned HTTP 501. Catch the exception and fall
back to a normal S3 call (see [Error Handling and Fallback](#error-handling-and-fallback)).

#### Connection error
```
RDMAConnectionError: cuObjClient created but not connected
```
**Solution:** Check RDMA network connectivity and provider service.

---

## Unlimited Tagging

AWS S3 limits objects to 10 tags. Infinia supports additional "unlimited" tags, identified by a
configurable key prefix (default `ddn:`). Writing them needs no plugin: use a `ddn:`-prefixed key in a
normal `put_object_tagging` call. Reading and deleting them requires extra `x-ddn-tagging-*` request
headers, which `Boto3TaggingPlugin` adds via new `Unlimited`, `Prefix`, `MaxTags` and `Marker`
parameters on the tagging calls.

```python
import boto3
from boto3_infinia.tagging import Boto3TaggingPlugin

s3 = boto3.client('s3')
plugin = Boto3TaggingPlugin(s3)

s3.put_object_tagging(
    Bucket='my-bucket', Key='my-object',
    Tagging={'TagSet': [{'Key': 'ddn:app.env', 'Value': 'prod'}]},
)

# Read AWS + Infinia tags, 100 per page, filtered by prefix
response = s3.get_object_tagging(
    Bucket='my-bucket', Key='my-object',
    Unlimited=True, MaxTags=100, Prefix='ddn:app',
)
```

See [TAGGING_PLUGIN_README.md](TAGGING_PLUGIN_README.md) for the full parameter reference, pagination,
deletion semantics, and error handling, and
[`examples/ddn_unlimited_tagging_example.py`](examples/ddn_unlimited_tagging_example.py) for runnable examples.

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Copyright 2026 DataDirect Networks, Inc.

## Contributing

Contributions welcome! Please open an issue or pull request.
