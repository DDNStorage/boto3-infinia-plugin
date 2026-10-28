# Boto3 Unlimited Tagging Plugin

A non-intrusive plugin for boto3 that adds unlimited S3 object tagging support beyond AWS's 10-tag limit.

## Overview

This plugin extends boto3's S3 client to support DDN's unlimited tagging feature by:
1. Accepting additional tagging-specific parameters (`Unlimited`, `MaxTags`, `Prefix`, `Marker`)
2. Injecting custom headers into S3 API requests before signing
3. Surfacing pagination response headers (`IsTruncated`/`NextMarker`) on GET responses
4. Working seamlessly alongside the RDMA plugin

## Installation

```bash
# Install the package (includes both RDMA and Unlimited Tagging plugins)
pip install dist/boto3_infinia_plugin-*.whl  # built with `uv build`, see README

# Or install in development mode
pip install -e .
```

## Quick Start

```python
import boto3
from boto3_infinia.tagging import Boto3TaggingPlugin

# Create S3 client
s3 = boto3.client(
    's3',
    endpoint_url='https://your-infinia-server:8111',
    aws_access_key_id='YOUR_ACCESS_KEY',
    aws_secret_access_key='YOUR_SECRET_KEY',
    region_name='us-east-1',
    verify=False  # For self-signed certificates
)

# Enable unlimited tagging plugin
plugin = Boto3TaggingPlugin(s3)

# Writing DDN-unlimited tags needs no plugin/header: just use a tag key
# with the 'ddn:' prefix in a normal put_object_tagging call.
s3.put_object_tagging(
    Bucket='my-bucket',
    Key='my-object',
    Tagging={'TagSet': [{'Key': 'ddn:app.env', 'Value': 'prod'}]},
)

# Reading/deleting DDN tags needs the extra parameters below, since plain
# boto3 has no way to express the required x-ddn-tagging-* headers.
response = s3.get_object_tagging(
    Bucket='my-bucket',
    Key='my-object',
    Unlimited=True,
    MaxTags=100,
    Prefix='ddn:app'
)
```

## Features

### 1. Get Unlimited Tags

#### Standard Request (Backward Compatible)
```python
# Works like standard boto3 - returns AWS tags only
response = s3.get_object_tagging(
    Bucket='testbucket',
    Key='file.txt'
)
```

#### Unlimited Mode
```python
# Get AWS + DDN tags
response = s3.get_object_tagging(
    Bucket='testbucket',
    Key='file.txt',
    Unlimited=True
)
print(f"Got {len(response['TagSet'])} tags")
```

#### Pagination
```python
# Get DDN tags in pages
response = s3.get_object_tagging(
    Bucket='testbucket',
    Key='file.txt',
    Unlimited=True,
    MaxTags=10  # Get 10 tags at a time
)

# Check for more pages
if response.get('IsTruncated'):
    next_response = s3.get_object_tagging(
        Bucket='testbucket',
        Key='file.txt',
        Unlimited=True,
        MaxTags=10,
        Marker=response['NextMarker']
    )
```

#### Prefix Filtering
```python
# Get only DDN tags starting with 'ddn:app' (requires Unlimited=True)
# Note: Prefix MUST start with 'ddn:' (server validation)
response = s3.get_object_tagging(
    Bucket='testbucket',
    Key='file.txt',
    Unlimited=True,
    Prefix='ddn:app'
)
```

### 2. Write Unlimited Tags

No plugin/header needed: DDN opt-in is the `ddn:` key prefix itself.

```python
s3.put_object_tagging(
    Bucket='testbucket',
    Key='file.txt',
    Tagging={'TagSet': [
        {'Key': 'Environment', 'Value': 'Production'},  # AWS tag
        {'Key': 'ddn:app.version', 'Value': '1.2.3'},   # DDN unlimited tag
    ]},
)
```

### 3. Delete Tags

#### Standard Delete
```python
# Delete AWS tags only; DDN tags are preserved
s3.delete_object_tagging(
    Bucket='testbucket',
    Key='file.txt'
)
```

#### Delete All Unlimited Tags
```python
# Delete AWS tags + ALL DDN tags
s3.delete_object_tagging(
    Bucket='testbucket',
    Key='file.txt',
    Unlimited=True
)
```

#### Delete Tags by Prefix
```python
# Delete AWS tags + DDN tags matching prefix 'ddn:app'
# Example: Deletes 'ddn:app.version', 'ddn:app.env', etc.
s3.delete_object_tagging(
    Bucket='testbucket',
    Key='file.txt',
    Unlimited=True,
    Prefix='ddn:app'
)
```

## Parameters

### get_object_tagging / get_bucket_tagging

| Parameter | Type | Description |
|-----------|------|-------------|
| `Unlimited` | `bool` | Include DDN tags alongside AWS tags |
| `MaxTags` | `int` | Max DDN tags per page, 1-1000 (requires `Unlimited=True`) |
| `Marker` | `str` | Resume after a prior page's `NextMarker` (requires `Unlimited=True`) |
| `Prefix` | `str` | Filter DDN tags by prefix, must start with `ddn:` (requires `Unlimited=True`) |

Response adds `IsTruncated`/`NextMarker` when more DDN tags are available.

### delete_object_tagging / delete_bucket_tagging

| Parameter | Type | Description |
|-----------|------|-------------|
| `Unlimited` | `bool` | `True` deletes AWS tags + all DDN tags; omitted deletes AWS tags only |
| `Prefix` | `str` | Delete only DDN tags matching this prefix (requires `Unlimited=True`) |

## Custom Headers

The plugin adds these custom headers to GET/DELETE requests:

| Header | Added When | Value |
|--------|------------|-------|
| `x-ddn-tagging-unlimited` | `Unlimited` provided | `'true'` or `'false'` |
| `x-ddn-tagging-prefix` | `Prefix` provided | Prefix string |
| `x-ddn-tagging-max-tags` | `MaxTags` provided (GET only) | Number of tags |
| `x-ddn-tagging-marker` | `Marker` provided (GET only) | Marker string |

The plugin also reads these response headers on GET and surfaces them as `IsTruncated`/`NextMarker` in the parsed response:

| Response Header | Surfaced As |
|------------------|-------------|
| `x-ddn-tagging-truncated` | `IsTruncated` |
| `x-ddn-tagging-next-marker` | `NextMarker` |

Writing DDN tags requires no custom header — use a tag key with the `ddn:` prefix in a standard `put_object_tagging`/`put_bucket_tagging` call.

## Working with RDMA Plugin

Both plugins work independently and can be used together:

```python
from boto3_infinia.rdma import Boto3RdmaPlugin
from boto3_infinia.tagging import Boto3TaggingPlugin

s3 = boto3.client('s3', endpoint_url='https://...')

# Enable both plugins
rdma_plugin = Boto3RdmaPlugin(s3)
tagging_plugin = Boto3TaggingPlugin(s3)

# Use RDMA for data operations
import numpy as np
buffer = np.zeros(1024*1024, dtype=np.uint8)
s3.get_object(Bucket='bucket', Key='file', buffer=buffer)

# Use unlimited tagging for tag operations
s3.get_object_tagging(Bucket='bucket', Key='file', Unlimited=True)
```

## Error Handling

```python
from boto3_infinia.tagging import TaggingNotSupportedError

try:
    response = s3.get_object_tagging(
        Bucket='bucket',
        Key='file',
        Unlimited=True
    )
except TaggingNotSupportedError:
    # Server doesn't support unlimited tagging
    # Fallback to standard tagging
    response = s3.get_object_tagging(Bucket='bucket', Key='file')
```

## Examples

See `examples/ddn_unlimited_tagging_example.py` for comprehensive usage examples.

## Server Requirements

- DDN Infinia server with unlimited tagging support
- Server must accept custom DDN headers (`x-ddn-*`)
- Prefix filtering requires prefix to start with `ddn:` (server validation)

## License

Apache License 2.0
