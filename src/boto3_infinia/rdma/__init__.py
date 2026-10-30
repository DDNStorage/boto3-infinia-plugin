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
Boto3 RDMA cuObjClient Plugin

A non-intrusive RDMA acceleration layer for boto3 S3 operations.
Adds RDMA support without modifying boto3, s3transfer, aws-crt-python, or aws-c-s3.

Usage:
    import boto3
    from boto3_infinia.rdma import Boto3RdmaPlugin

    s3 = boto3.client('s3')
    plugin = Boto3RdmaPlugin(s3)

    # Now you can use buffer parameter
    import numpy as np
    buffer = np.zeros(1024*1024, dtype=np.uint8)
    s3.get_object(Bucket='my-bucket', Key='my-key', buffer=buffer)
"""

from .plugin import Boto3RdmaPlugin
from .exceptions import (
    RDMAPluginError,
    RDMANotSupportedError,
    RDMAConnectionError,
    RDMATokenError,
    RDMALibraryNotFoundError,
)

__version__ = "2.5.1"
__all__ = [
    "Boto3RdmaPlugin",
    "RDMAPluginError",
    "RDMANotSupportedError",
    "RDMAConnectionError",
    "RDMATokenError",
    "RDMALibraryNotFoundError",
]
