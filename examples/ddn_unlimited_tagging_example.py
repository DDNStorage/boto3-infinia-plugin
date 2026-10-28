#!/usr/bin/env python3
# Copyright 2026 DataDirect Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Example usage of Boto3TaggingPlugin for DDN unlimited S3 tagging.

Demonstrates writing DDN-unlimited tags (no plugin needed - just use the
configured key prefix, default 'ddn:') and reading/deleting them via the
plugin's extra 'Unlimited'/'Prefix'/'MaxTags'/'Marker' parameters.
"""

import os
import warnings

import urllib3

# Suppress the InsecureRequestWarning noise caused by verify=False
# (self-signed certs on test servers).
warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

import boto3
from boto3_infinia.tagging import Boto3TaggingPlugin


def main():
    # Credentials/endpoint are picked up from the standard AWS env vars
    # (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3, AWS_REGION)
    # if set; otherwise falls back to the placeholders below.
    s3 = boto3.client(
        's3',
        endpoint_url=os.environ.get('AWS_ENDPOINT_URL_S3', 'https://your-infinia-server:8111'),
        region_name=os.environ.get('AWS_REGION', 'us-east-1'),
        verify=False,  # For self-signed certificates
    )

    plugin = Boto3TaggingPlugin(s3)

    bucket = os.environ.get('S3_BUCKET', 'my-bucket')
    key = 'my-object'

    # Create the object first - tagging APIs require the object to exist.
    print("Setup: Creating object")
    print("-" * 50)
    s3.put_object(Bucket=bucket, Key=key, Body=b'hello world')
    print(f"Created object '{key}' in bucket '{bucket}'\n")

    # Example 1: Put AWS tags + DDN-unlimited tags in one request.
    # No plugin/header needed for PUT: DDN opt-in is the 'ddn:' key prefix.
    print("Example 1: Put AWS + unlimited DDN tags")
    print("-" * 50)
    tags = [
        {'Key': 'Environment', 'Value': 'Production'},
        {'Key': 'Owner', 'Value': 'TeamA'},
    ] + [
        {'Key': f'ddn:app.setting{i}', 'Value': f'value{i}'}
        for i in range(20)  # Unlimited count, no AWS 10-tag cap
    ]
    s3.put_object_tagging(Bucket=bucket, Key=key, Tagging={'TagSet': tags})
    print(f"Put {len(tags)} tags successfully!\n")

    # Example 2: Default GET - AWS-compliant tags only (naive-client safe)
    print("Example 2: Default GET (AWS tags only)")
    print("-" * 50)
    response = s3.get_object_tagging(Bucket=bucket, Key=key)
    print(f"AWS tags: {response['TagSet']}\n")

    # Example 3: GET with Unlimited=True - AWS + DDN tags
    print("Example 3: GET with Unlimited=True")
    print("-" * 50)
    response = s3.get_object_tagging(Bucket=bucket, Key=key, Unlimited=True)
    print(f"Retrieved {len(response['TagSet'])} tags: {response['TagSet']}\n")

    # Example 4: Paginate DDN tags with MaxTags + Marker
    print("Example 4: GET with pagination")
    print("-" * 50)
    response = s3.get_object_tagging(
        Bucket=bucket, Key=key, Unlimited=True, MaxTags=10,
    )
    print(f"First page: {len(response['TagSet'])} tags")
    if response.get('IsTruncated'):
        next_marker = response['NextMarker']
        print(f"More tags available, next marker: {next_marker}")
        response = s3.get_object_tagging(
            Bucket=bucket, Key=key, Unlimited=True, MaxTags=10, Marker=next_marker,
        )
        print(f"Second page: {len(response['TagSet'])} tags\n")

    # Example 5: Filter DDN tags by prefix (requires Unlimited=True)
    print("Example 5: GET with Prefix filter")
    print("-" * 50)
    response = s3.get_object_tagging(
        Bucket=bucket, Key=key, Unlimited=True, Prefix='ddn:app.setting1',
    )
    print(f"Filtered tags: {response['TagSet']}\n")

    # Example 6: Default DELETE - removes AWS tags only, DDN tags preserved
    print("Example 6: Default DELETE (AWS tags only)")
    print("-" * 50)
    s3.delete_object_tagging(Bucket=bucket, Key=key)
    print("AWS tags deleted; DDN tags preserved.\n")

    # Example 7: Delete AWS tags + all DDN tags
    print("Example 7: DELETE with Unlimited=True")
    print("-" * 50)
    s3.delete_object_tagging(Bucket=bucket, Key=key, Unlimited=True)
    print("AWS tags + all DDN tags deleted!\n")

    # Example 8: Delete AWS tags + DDN tags under a specific prefix
    print("Example 8: DELETE with Prefix filter")
    print("-" * 50)
    s3.put_object_tagging(Bucket=bucket, Key=key, Tagging={'TagSet': tags})
    s3.delete_object_tagging(
        Bucket=bucket, Key=key, Unlimited=True, Prefix='ddn:app.setting1',
    )
    print("AWS tags + matching DDN tags deleted!\n")

    # Example 9: Bucket tagging works the same way
    print("Example 9: Bucket tagging (Unlimited=True)")
    print("-" * 50)
    s3.put_bucket_tagging(Bucket=bucket, Tagging={'TagSet': tags})
    response = s3.get_bucket_tagging(Bucket=bucket, Unlimited=True)
    print(f"Bucket tags: {response['TagSet']}\n")


if __name__ == '__main__':
    main()
