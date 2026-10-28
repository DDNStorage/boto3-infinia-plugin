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
Exception classes for boto3_infinia.tagging
"""


class TaggingPluginError(Exception):
    """Base exception for all unlimited tagging plugin errors"""

    pass


class TaggingNotSupportedError(TaggingPluginError):
    """
    Raised when unlimited tagging is not supported by the server.

    This typically happens when the server doesn't recognize the
    ``x-ddn-tagging-*`` headers and returns an error (e.g. HTTP 501).
    """

    pass


class TaggingValidationError(TaggingPluginError):
    """
    Raised client-side when unlimited tagging parameters are invalid.

    Mirrors the server-side ``InvalidArgument`` validation (e.g. ``Prefix``/``MaxTags``/``Marker`` supplied without
    ``Unlimited=True``), so callers get fast, local feedback instead of a
    round trip to the server.
    """

    pass
