# Boto3 RDMA cuObjClient Plugin - API Reference

## Exception Hierarchy

All exceptions inherit from `RDMAPluginError`, so you can catch any plugin error with a single
`except RDMAPluginError:` clause:

```
RDMAPluginError
├── RDMANotSupportedError    # Server returned HTTP 501 (RDMA not supported)
├── RDMAConnectionError      # cuObjClient failed to connect to RDMA provider
├── RDMATokenError           # RDMA token generation/registration failed
└── RDMALibraryNotFoundError # cuobjclient/cppyy not found or failed to load
```

## Class: Boto3RdmaPlugin

### Constructor

```python
Boto3RdmaPlugin(s3_client)
```

Initialize the RDMA plugin and patch the S3 client.

**Parameters:**
- `s3_client`: boto3 S3 client instance

**Raises:**
- `RDMALibraryNotFoundError`: If cuobjclient is not found
- `RDMAConnectionError`: If cuObjClient connection fails

**Example:**
```python
import boto3
from boto3_infinia.rdma import Boto3RdmaPlugin

s3 = boto3.client('s3')
plugin = Boto3RdmaPlugin(s3)
```

---

## Buffer Registration Methods

### register_buffer()

```python
plugin.register_buffer(buffer)
```

Register a buffer with the RDMA system for later use with `is_buffer_registered=True`.

**Parameters:**
- `buffer`: Buffer to register. Supported types:
  - NumPy array (`numpy.ndarray`)
  - CuPy array (`cupy.ndarray`)
  - PyTorch tensor (`torch.Tensor`, CPU or GPU; must be contiguous)
  - Any object supporting Python buffer protocol

**Returns:** `None`

**Raises:**
- `ValueError`: If buffer type is unsupported, or a PyTorch tensor is not contiguous
- `RDMATokenError`: If registration fails

**Features:**
- ✅ Automatic buffer type detection
- ✅ Automatic size calculation
- ✅ Automatic pointer conversion (GPU/CPU)
- ✅ No manual address extraction needed

**Example:**
```python
import numpy as np

buffer = np.zeros(10 * 1024 * 1024, dtype=np.uint8)
plugin.register_buffer(buffer)

# Now use with is_buffer_registered=True
for i in range(100):
    s3.get_object(Bucket='bucket', Key=f'file{i}',
                  buffer=buffer, is_buffer_registered=True)

plugin.unregister_buffer(buffer)
```

---

### unregister_buffer()

```python
plugin.unregister_buffer(buffer)
```

Unregister a previously registered buffer and free RDMA resources.

**Parameters:**
- `buffer`: Buffer to unregister (must be the same object passed to `register_buffer()`)

**Returns:** `None`

**Raises:**
- `ValueError`: If buffer type is unsupported, or a PyTorch tensor is not contiguous

**Important:** Always call this when done using a pre-registered buffer to avoid resource leaks.

**Example:**
```python
plugin.unregister_buffer(buffer)
```

---

### registered_buffer()

```python
with plugin.registered_buffer(buffer):
    # Use buffer here
    ...
```

Context manager for automatic buffer registration and cleanup.

**Parameters:**
- `buffer`: Buffer to register (NumPy array, CuPy array, PyTorch tensor, or buffer protocol object)

**Yields:** The same buffer object (for convenience)

**Raises:**
- `ValueError`: If buffer type is unsupported, or a PyTorch tensor is not contiguous
- `RDMATokenError`: If registration fails

**Description:**
The context manager automatically:
1. Registers the buffer on entry (`__enter__`)
2. Yields the buffer for use
3. Unregisters the buffer on exit (`__exit__`), even if an exception occurs

This is the recommended way to use pre-registered buffers as it guarantees cleanup.

**Example:**
```python
import numpy as np

buffer = np.zeros(10 * 1024 * 1024, dtype=np.uint8)

# Automatic registration and cleanup
with plugin.registered_buffer(buffer):
    for i in range(100):
        s3.get_object(Bucket='bucket', Key=f'file{i}',
                      buffer=buffer, is_buffer_registered=True)
# Buffer is automatically unregistered here, even if exception occurred
```

**Comparison with manual registration:**
```python
# Manual (more error-prone)
plugin.register_buffer(buffer)
try:
    # ... use buffer ...
finally:
    plugin.unregister_buffer(buffer)

# Context manager (recommended)
with plugin.registered_buffer(buffer):
    # ... use buffer ...
```

---

## Patched S3 Methods

The plugin monkey-patches these S3 client methods to accept additional parameters:

### get_object()

```python
s3.get_object(Bucket='...', Key='...', buffer=None, is_buffer_registered=False, **kwargs)
```

**Additional Parameters:**
- `buffer`: Buffer to receive downloaded data (optional)
- `is_buffer_registered`: Set to `True` if buffer was pre-registered (default: `False`)

**Raises:**
- `RDMANotSupportedError`: If `buffer` is set and the server returns HTTP 501 (RDMA not
  supported); catch this and retry without `buffer` for a normal S3 transfer
- `RDMATokenError`: If RDMA token generation/registration fails while preparing the request

**Example:**
```python
import numpy as np

buffer = np.zeros(10*1024*1024, dtype=np.uint8)
response = s3.get_object(Bucket='bucket', Key='file.bin', buffer=buffer)
# Data is now in buffer
```

---

### put_object()

```python
s3.put_object(Bucket='...', Key='...', buffer=None, is_buffer_registered=False, **kwargs)
```

**Additional Parameters:**
- `buffer`: Buffer containing data to upload (optional)
- `is_buffer_registered`: Set to `True` if buffer was pre-registered (default: `False`)

**Note:** When `buffer` is provided, any `Body` and `ContentLength` kwargs you pass are
overridden — the wrapper always sends `Body=b""` and `ContentLength=0`, since the actual
payload transfers out-of-band via RDMA.

**Raises:**
- `RDMANotSupportedError`: If `buffer` is set and the server returns HTTP 501 (RDMA not
  supported); catch this and retry without `buffer` for a normal S3 transfer
- `RDMATokenError`: If RDMA token generation/registration fails while preparing the request

**Example:**
```python
import numpy as np

data = np.random.randint(0, 256, size=10*1024*1024, dtype=np.uint8)
response = s3.put_object(Bucket='bucket', Key='file.bin', buffer=data)
```

---

### upload_part()

```python
s3.upload_part(Bucket='...', Key='...', UploadId='...', PartNumber=1, 
               buffer=None, is_buffer_registered=False, **kwargs)
```

**Additional Parameters:**
- `buffer`: Buffer containing part data to upload (optional)
- `is_buffer_registered`: Set to `True` if buffer was pre-registered (default: `False`)

**Note:** When `buffer` is provided, any `Body` and `ContentLength` kwargs you pass are
overridden — the wrapper always sends `Body=b""` and `ContentLength=0`, since the actual
payload transfers out-of-band via RDMA.

**Raises:**
- `RDMANotSupportedError`: If `buffer` is set and the server returns HTTP 501 (RDMA not
  supported); catch this and retry without `buffer` for a normal S3 transfer
- `RDMATokenError`: If RDMA token generation/registration fails while preparing the request

**Example:**
```python
mpu = s3.create_multipart_upload(Bucket='bucket', Key='large-file.bin')
upload_id = mpu['UploadId']

parts = []
for i in range(1, 4):
    part_data = np.random.randint(0, 256, size=5*1024*1024, dtype=np.uint8)
    response = s3.upload_part(
        Bucket='bucket', Key='large-file.bin',
        UploadId=upload_id, PartNumber=i,
        buffer=part_data
    )
    parts.append({'PartNumber': i, 'ETag': response['ETag']})

s3.complete_multipart_upload(
    Bucket='bucket', Key='large-file.bin',
    UploadId=upload_id, MultipartUpload={'Parts': parts}
)
```

---

## Supported Buffer Types

| Type | Module | CPU/GPU | Notes |
|------|--------|---------|-------|
| NumPy array | `numpy` | CPU | Most common use case |
| CuPy array | `cupy` | GPU | Zero-copy GPU transfers |
| PyTorch tensor | `torch` | CPU or GPU | Must be contiguous (call `.contiguous()` if needed) |
| Buffer protocol | Built-in | CPU | `bytes`, `bytearray`, etc. |

---

## Performance Optimization

For buffers used multiple times, pre-registration eliminates overhead:

```python
# Without pre-registration (slower)
for i in range(100):
    s3.get_object(Bucket='bucket', Key=f'file{i}', buffer=buffer)
    # Registers/unregisters on each call

# With pre-registration (faster)
plugin.register_buffer(buffer)
for i in range(100):
    s3.get_object(Bucket='bucket', Key=f'file{i}', 
                  buffer=buffer, is_buffer_registered=True)
plugin.unregister_buffer(buffer)
```
