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
Boto3 Unlimited Tagging Plugin

A non-intrusive unlimited S3 tagging layer for boto3 S3 operations.
Adds DDN unlimited tagging support (headers only) without modifying boto3.

Writing DDN-unlimited tags requires no plugin and no special header: just
use a tag key starting with the configured prefix (default ``ddn:``) in a
normal ``put_object_tagging`` / ``put_bucket_tagging`` call. This plugin
adds the ``x-ddn-tagging-*`` request headers needed to read/delete the
unlimited tag set, which plain boto3 has no way to express.

Usage:
    import boto3
    from boto3_infinia.tagging import Boto3TaggingPlugin

    s3 = boto3.client('s3')
    plugin = Boto3TaggingPlugin(s3)

    # Read AWS + DDN tags (paginated)
    s3.get_object_tagging(
        Bucket='my-bucket',
        Key='my-key',
        Unlimited=True,
        MaxTags=100,
        Prefix='ddn:app',
    )

    # Delete AWS tags and all DDN tags
    s3.delete_object_tagging(
        Bucket='my-bucket',
        Key='my-key',
        Unlimited=True,
    )
"""

from .plugin import Boto3TaggingPlugin
from .exceptions import (
    TaggingPluginError,
    TaggingNotSupportedError,
    TaggingValidationError,
)

__version__ = "2.5.1"
__all__ = [
    "Boto3TaggingPlugin",
    "TaggingPluginError",
    "TaggingNotSupportedError",
    "TaggingValidationError",
]
