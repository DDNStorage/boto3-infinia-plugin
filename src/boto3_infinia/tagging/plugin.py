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
Main plugin implementation for DDN Unlimited Tagging.

Implements the client side of Infinia unlimited tagging:
  - PUT needs no plugin: a tag key with the configured prefix (default
    ``ddn:``) is routed to unlimited storage by the server automatically.
  - GET/DELETE need the ``x-ddn-tagging-*`` request headers below to opt
    into returning/removing the DDN tag set, since plain boto3 has no way
    to express them.
"""

import contextvars
import logging
from functools import wraps

from botocore.exceptions import ClientError

from .exceptions import TaggingNotSupportedError, TaggingValidationError

logger = logging.getLogger(__name__)

# Context variable to pass tagging parameters between wrapper and event handlers
tagging_ctx = contextvars.ContextVar("ddn_tagging", default=None)

# Request headers
HEADER_UNLIMITED = "x-ddn-tagging-unlimited"
HEADER_PREFIX = "x-ddn-tagging-prefix"
HEADER_MAX_TAGS = "x-ddn-tagging-max-tags"
HEADER_MARKER = "x-ddn-tagging-marker"

# Response headers (GET only, present when x-ddn-tagging-unlimited: true)
HEADER_TRUNCATED = "x-ddn-tagging-truncated"
HEADER_NEXT_MARKER = "x-ddn-tagging-next-marker"

_GET_OPERATIONS = {
    "get_object_tagging": ("GetObjectTagging", True),
    "get_bucket_tagging": ("GetBucketTagging", False),
}
_DELETE_OPERATIONS = {
    "delete_object_tagging": ("DeleteObjectTagging", True),
    "delete_bucket_tagging": ("DeleteBucketTagging", False),
}


def _normalize_bool_header(value):
    """Convert a bool/str Unlimited value into the 'true'/'false' header value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower()
    raise TaggingValidationError(
        f"Unlimited must be a bool or 'true'/'false' string, got {value!r}"
    )


class Boto3TaggingPlugin:
    """
    Boto3 Tagging plugin for DDN unlimited S3 tagging operations.

    This plugin adds unlimited tagging support to boto3 by:
    1. Monkey-patching S3 client tagging methods to accept extra parameters
    2. Using boto3's event system to inject the ``x-ddn-tagging-*`` headers
    3. Surfacing the server's pagination response headers on GET responses

    Example:
        s3 = boto3.client('s3')
        plugin = Boto3TaggingPlugin(s3)

        # Get AWS + DDN tags with pagination
        s3.get_object_tagging(
            Bucket='bucket',
            Key='key',
            Unlimited=True,
            MaxTags=100,
            Prefix='ddn:app',
        )

        # Delete AWS tags and all DDN tags
        s3.delete_object_tagging(
            Bucket='bucket',
            Key='key',
            Unlimited=True,
        )
    """

    def __init__(self, s3_client):
        """
        Initialize the Boto3 Tagging plugin and patch the S3 client.

        Args:
            s3_client: boto3 S3 client instance

        Example:
            >>> import boto3
            >>> from boto3_infinia.tagging import Boto3TaggingPlugin
            >>> s3 = boto3.client('s3', endpoint_url='https://...')
            >>> plugin = Boto3TaggingPlugin(s3)
        """
        self.client = s3_client

        logger.info("Initializing Boto3 Tagging Plugin")

        self._register_event_handlers()
        self._patch_methods()

        logger.info("Boto3 Tagging Plugin initialized successfully")

    def _register_event_handlers(self):
        """Register event handlers on the S3 client's event system."""
        event_system = self.client.meta.events

        for method_name, (op_name, _) in _GET_OPERATIONS.items():
            event_system.register_first(
                f"before-sign.s3.{op_name}",
                self._add_get_tagging_headers,
                unique_id=f"ddn-tagging-{op_name}",
            )
            event_system.register(
                f"after-call.s3.{op_name}",
                self._add_get_tagging_response_fields,
                unique_id=f"ddn-tagging-response-{op_name}",
            )

        for method_name, (op_name, _) in _DELETE_OPERATIONS.items():
            event_system.register_first(
                f"before-sign.s3.{op_name}",
                self._add_delete_tagging_headers,
                unique_id=f"ddn-tagging-{op_name}",
            )

        logger.debug("Event handlers registered")

    def _patch_methods(self):
        """Monkey-patch S3 client tagging methods to accept DDN parameters."""
        self._orig_methods = {}

        for method_name in list(_GET_OPERATIONS) + list(_DELETE_OPERATIONS):
            orig = getattr(self.client, method_name)
            self._orig_methods[method_name] = orig

        for method_name in _GET_OPERATIONS:
            setattr(self.client, method_name, self._create_get_wrapper(method_name))

        for method_name in _DELETE_OPERATIONS:
            setattr(self.client, method_name, self._create_delete_wrapper(method_name))

        logger.debug("S3 client tagging methods patched")

    def _create_get_wrapper(self, method_name):
        """Create a wrapped get_*_tagging method (ObjectTagging or BucketTagging)."""
        orig_method = self._orig_methods[method_name]

        @wraps(orig_method)
        def get_tagging_wrapper(**kwargs):
            """
            Wrapper for get_object_tagging/get_bucket_tagging that accepts DDN
            unlimited tagging parameters.

            Args:
                **kwargs: Standard parameters plus optional:
                    - Unlimited (bool): Include DDN tags alongside AWS tags
                    - MaxTags (int): Max DDN tags per page (1-1000, default 1000)
                    - Prefix (str): Filter DDN keys (requires Unlimited=True)
                    - Marker (str): Resume after a prior page's next marker

            Returns:
                Standard response, plus 'IsTruncated'/'NextMarker' when the
                server reports more DDN tags are available.
            """
            unlimited = kwargs.pop("Unlimited", None)
            max_tags = kwargs.pop("MaxTags", None)
            prefix = kwargs.pop("Prefix", None)
            marker = kwargs.pop("Marker", None)

            ctx = self._validate_get_params(unlimited, max_tags, prefix, marker)

            if ctx:
                logger.debug(f"{method_name} with {ctx}")
                tagging_ctx.set(ctx)

            try:
                return orig_method(**kwargs)
            except ClientError as e:
                raise self._translate_not_supported(e, unlimited) from None
            finally:
                if ctx:
                    tagging_ctx.set(None)

        return get_tagging_wrapper

    def _create_delete_wrapper(self, method_name):
        """Create a wrapped delete_*_tagging method (ObjectTagging or BucketTagging)."""
        orig_method = self._orig_methods[method_name]

        @wraps(orig_method)
        def delete_tagging_wrapper(**kwargs):
            """
            Wrapper for delete_object_tagging/delete_bucket_tagging that
            accepts DDN unlimited tagging parameters.

            Args:
                **kwargs: Standard parameters plus optional:
                    - Unlimited (bool or 'true'/'false'): 'true' deletes AWS
                      tags and all DDN tags; 'false'/omitted deletes AWS
                      tags only (DDN tags are preserved).
                    - Prefix (str): Delete only DDN keys with this prefix
                      (requires Unlimited=True)

            Returns:
                Standard delete_*_tagging response
            """
            unlimited = kwargs.pop("Unlimited", None)
            prefix = kwargs.pop("Prefix", None)

            ctx = self._validate_delete_params(unlimited, prefix)

            if ctx:
                logger.debug(f"{method_name} with {ctx}")
                tagging_ctx.set(ctx)

            try:
                return orig_method(**kwargs)
            except ClientError as e:
                raise self._translate_not_supported(e, unlimited) from None
            finally:
                if ctx:
                    tagging_ctx.set(None)

        return delete_tagging_wrapper

    def _validate_get_params(self, unlimited, max_tags, prefix, marker):
        """Validate GET params client-side and build the header context dict."""
        if max_tags is None and prefix is None and marker is None and unlimited is None:
            return None

        if (max_tags is not None or prefix is not None or marker is not None) and not unlimited:
            raise TaggingValidationError(
                "Prefix/MaxTags/Marker require Unlimited=True"
            )

        if max_tags is not None and not (1 <= int(max_tags) <= 1000):
            raise TaggingValidationError("MaxTags must be between 1 and 1000")

        ctx = {}
        if unlimited is not None:
            ctx["Unlimited"] = _normalize_bool_header(unlimited)
        if prefix is not None:
            ctx["Prefix"] = prefix
        if max_tags is not None:
            ctx["MaxTags"] = str(int(max_tags))
        if marker is not None:
            ctx["Marker"] = marker
        return ctx

    def _validate_delete_params(self, unlimited, prefix):
        """Validate DELETE params client-side and build the header context dict."""
        if unlimited is None and prefix is None:
            return None

        if prefix is not None and not unlimited:
            raise TaggingValidationError("Prefix requires Unlimited=True")

        ctx = {}
        if unlimited is not None:
            ctx["Unlimited"] = _normalize_bool_header(unlimited)
        if prefix is not None:
            ctx["Prefix"] = prefix
        return ctx

    def _translate_not_supported(self, error, requested_unlimited):
        """Translate a 501/UnlimitedTagging ClientError into a friendlier error."""
        if requested_unlimited:
            error_code = error.response.get("Error", {}).get("Code", "")
            status_code = error.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode", 0
            )
            if "UnlimitedTagging" in error_code or status_code == 501:
                return TaggingNotSupportedError(
                    "Unlimited tagging not supported by server. "
                    "Retry without the Unlimited parameter."
                )
        return error

    def _add_get_tagging_headers(self, request, **kwargs):
        """'before-sign' handler for GetObjectTagging/GetBucketTagging."""
        ctx = tagging_ctx.get()
        if not ctx:
            return

        if "Unlimited" in ctx:
            request.headers.add_header(HEADER_UNLIMITED, ctx["Unlimited"])
        if "Prefix" in ctx:
            request.headers.add_header(HEADER_PREFIX, ctx["Prefix"])
        if "MaxTags" in ctx:
            request.headers.add_header(HEADER_MAX_TAGS, ctx["MaxTags"])
        if "Marker" in ctx:
            request.headers.add_header(HEADER_MARKER, ctx["Marker"])
        logger.debug(f"Added DDN tagging request headers: {ctx}")

    def _add_delete_tagging_headers(self, request, **kwargs):
        """'before-sign' handler for DeleteObjectTagging/DeleteBucketTagging."""
        ctx = tagging_ctx.get()
        if not ctx:
            return

        if "Unlimited" in ctx:
            request.headers.add_header(HEADER_UNLIMITED, ctx["Unlimited"])
        if "Prefix" in ctx:
            request.headers.add_header(HEADER_PREFIX, ctx["Prefix"])
        logger.debug(f"Added DDN tagging request headers: {ctx}")

    def _add_get_tagging_response_fields(self, http_response, parsed, **kwargs):
        """
        'after-call' handler for GetObjectTagging/GetBucketTagging.

        Surfaces the DDN pagination response headers
        (x-ddn-tagging-truncated / x-ddn-tagging-next-marker) on the parsed
        response so callers don't need to inspect raw HTTP headers.
        """
        ctx = tagging_ctx.get()
        if not ctx or ctx.get("Unlimited") != "true" or parsed is None:
            return

        headers = getattr(http_response, "headers", {}) or {}
        truncated = headers.get(HEADER_TRUNCATED)
        next_marker = headers.get(HEADER_NEXT_MARKER)

        if truncated is not None:
            parsed["IsTruncated"] = truncated.lower() == "true"
        if next_marker:
            parsed["NextMarker"] = next_marker
