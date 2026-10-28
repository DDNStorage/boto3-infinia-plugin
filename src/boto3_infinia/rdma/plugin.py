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
Main plugin implementation
"""

import contextvars
import ctypes
import logging
import subprocess
import warnings
from functools import wraps
from typing import Optional, Any

from botocore.exceptions import ClientError

from .exceptions import (
    RDMANotSupportedError,
    RDMAConnectionError,
    RDMATokenError,
    RDMALibraryNotFoundError,
)

logger = logging.getLogger(__name__)

# Context variable to pass buffer information between wrapper and event handlers
buffer_ctx = contextvars.ContextVar("rdma_buffer", default=None)


class Boto3RdmaPlugin:
    """
    RDMA acceleration plugin for boto3 S3 operations.

    This plugin adds RDMA support to boto3 by:
    1. Monkey-patching S3 client methods to accept 'buffer' parameter
    2. Using boto3's event system to inject RDMA tokens into HTTP headers
    3. Managing RDMA buffer registration and cleanup

    Example:
        s3 = boto3.client('s3')
        plugin = Boto3RdmaPlugin(s3)

        import numpy as np
        buffer = np.zeros(1024*1024, dtype=np.uint8)
        s3.get_object(Bucket='bucket', Key='key', buffer=buffer)
    """

    def __init__(self, s3_client):
        """
        Initialize the RDMA plugin and patch the S3 client.

        Args:
            s3_client: boto3 S3 client instance

        Raises:
            RDMALibraryNotFoundError: If cuobjclient is not found
            RDMAConnectionError: If cuObjClient connection fails
        """
        self.client = s3_client

        # Load cuobjclient library (returns cppyy.gbl)
        self.gbl = self._load_cuobjclient()

        # Create cuObjClient instance with proper initialization
        # As per spec: new cuObjClient(ops, CUOBJ_PROTO_RDMA_DC_V1)
        logger.info("Creating cuObjClient instance...")

        # Create CUObjOps_t structure (empty)
        ops = self.gbl.CUObjOps_t()

        # Create cuObjClient with ops and protocol
        self.cuobjclient = self.gbl.cuObjClient(ops, self.gbl.CUOBJ_PROTO_RDMA_DC_V1)

        # Verify connection
        if not self.cuobjclient.isConnected():
            raise RDMAConnectionError(
                "cuObjClient created but not connected to RDMA provider. "
                "Check RDMA network connectivity and provider service."
            )

        logger.info("Boto3RdmaPlugin initialized successfully - cuObjClient connected")

        # Register event handlers
        self._register_event_handlers()

        # Monkey-patch S3 client methods
        self._patch_methods()

    def _get_buffer_info(self, buffer):
        """
        Extract buffer information (address, size, type) from various buffer types.

        Args:
            buffer: Buffer object (NumPy, CuPy, PyTorch, or buffer protocol)

        Returns:
            tuple: (buffer_ptr, size, buffer_type) where buffer_ptr is ready for cuObjClient

        Raises:
            ValueError: If buffer type is unsupported or tensor is not contiguous
        """
        if hasattr(buffer, "__cuda_array_interface__"):
            # CuPy GPU array (check first as it may also have __array_interface__)
            cuda_interface = buffer.__cuda_array_interface__
            buffer_addr = cuda_interface["data"][0]  # GPU memory address
            size = buffer.nbytes
            buffer_type = "CuPy (GPU)"

        elif hasattr(buffer, "data_ptr"):
            # PyTorch tensor (CPU or GPU)
            buffer_addr = buffer.data_ptr()  # Memory address
            size = buffer.element_size() * buffer.nelement()

            device_type = buffer.device.type if hasattr(buffer, "device") else "cpu"
            buffer_type = f"PyTorch ({device_type})"

            # Verify tensor is contiguous
            if not buffer.is_contiguous():
                raise ValueError(
                    "PyTorch tensor must be contiguous. "
                    "Call .contiguous() on the tensor before passing to RDMA."
                )

        elif hasattr(buffer, "__array_interface__"):
            # NumPy array (CPU only)
            # Use buffer object for CPU arrays - cppyy handles buffer protocol
            buffer_addr = buffer
            size = buffer.nbytes
            buffer_type = "NumPy (CPU)"

        else:
            # Try generic buffer protocol (CPU)
            try:
                memview = memoryview(buffer)
                buffer_addr = buffer
                size = memview.nbytes
                buffer_type = "buffer protocol (CPU)"
            except TypeError:
                raise ValueError(
                    f"Unsupported buffer type: {type(buffer).__name__}. "
                    "Buffer must support NumPy, CuPy, PyTorch, or Python buffer protocol."
                )

        # Convert to appropriate type for cppyy
        if isinstance(buffer_addr, int):
            # Integer address - create ctypes void pointer
            # This works for both GPU (CuPy, PyTorch CUDA) and CPU (PyTorch CPU)
            buffer_ptr = ctypes.c_void_p(buffer_addr)
        else:
            # Buffer protocol object (NumPy, bytes, etc.) - pass directly
            buffer_ptr = buffer_addr

        return buffer_ptr, size, buffer_type

    def register_buffer(self, buffer):
        """
        Register a buffer with the RDMA system.

        This allows pre-registration of buffers that will be used multiple times,
        avoiding the overhead of registration on each S3 operation.

        Args:
            buffer: Buffer to register (NumPy array, CuPy array, PyTorch tensor,
                   or any object supporting the buffer protocol)

        Returns:
            None

        Raises:
            ValueError: If buffer type is unsupported or tensor is not contiguous
            RDMATokenError: If registration fails

        Example:
            >>> import numpy as np
            >>> plugin = Boto3RdmaPlugin(s3)
            >>> buffer = np.zeros(10*1024*1024, dtype=np.uint8)
            >>>
            >>> # Pre-register buffer
            >>> plugin.register_buffer(buffer)
            >>>
            >>> # Use multiple times without re-registration overhead
            >>> for i in range(100):
            ...     s3.get_object(Bucket='bucket', Key=f'file{i}',
            ...                   buffer=buffer, is_buffer_registered=True)
            >>>
            >>> # Cleanup when done
            >>> plugin.unregister_buffer(buffer)
        """
        buffer_ptr, size, buffer_type = self._get_buffer_info(buffer)

        logger.debug(f"Registering buffer: type={buffer_type}, size={size}")

        # Call: cuMemObjGetDescriptor(buffer, size)
        err = self.cuobjclient.cuMemObjGetDescriptor(buffer_ptr, size)
        if err != self.gbl.CU_OBJ_SUCCESS:
            raise RDMATokenError(f"cuMemObjGetDescriptor failed with error code {err}")

        logger.debug(f"Buffer registered successfully")

    def unregister_buffer(self, buffer):
        """
        Unregister a previously registered buffer.

        This should be called when you're done using a pre-registered buffer
        to free RDMA resources.

        Args:
            buffer: Buffer to unregister (must be the same buffer object passed
                   to register_buffer)

        Returns:
            None

        Raises:
            ValueError: If buffer type is unsupported or tensor is not contiguous

        Example:
            >>> plugin.unregister_buffer(buffer)
        """
        buffer_ptr, size, buffer_type = self._get_buffer_info(buffer)

        logger.debug(f"Unregistering buffer: type={buffer_type}")

        # Call: cuMemObjPutDescriptor(buffer)
        self.cuobjclient.cuMemObjPutDescriptor(buffer_ptr)

        logger.debug(f"Buffer unregistered successfully")

    def registered_buffer(self, buffer):
        """
        Context manager for automatic buffer registration and cleanup.

        This provides a convenient way to register a buffer and ensure it gets
        unregistered even if an exception occurs.

        Args:
            buffer: Buffer to register (NumPy array, CuPy array, PyTorch tensor,
                   or any object supporting the buffer protocol)

        Yields:
            The same buffer object (for convenience)

        Raises:
            ValueError: If buffer type is unsupported or tensor is not contiguous
            RDMATokenError: If registration fails

        Example:
            >>> import numpy as np
            >>> plugin = Boto3RdmaPlugin(s3)
            >>> buffer = np.zeros(10*1024*1024, dtype=np.uint8)
            >>>
            >>> # Automatic registration and cleanup
            >>> with plugin.registered_buffer(buffer):
            ...     for i in range(100):
            ...         s3.get_object(Bucket='bucket', Key=f'file{i}',
            ...                       buffer=buffer, is_buffer_registered=True)
            >>> # Buffer is automatically unregistered here
        """
        from contextlib import contextmanager

        @contextmanager
        def _context():
            try:
                self.register_buffer(buffer)
                yield buffer
            finally:
                try:
                    self.unregister_buffer(buffer)
                except Exception as e:
                    logger.warning(f"Failed to unregister buffer during cleanup: {e}")

        return _context()

    def _load_cuobjclient(self):
        """
        Load cuobjclient library using pkgconfig and cppyy.

        Returns:
            Loaded cppyy.gbl module with cuObjClient accessible

        Raises:
            RDMALibraryNotFoundError: If library not found
            RDMAConnectionError: If connection fails
        """
        try:
            import cppyy

            # Store cppyy reference for use in event handlers
            self.cppyy = cppyy
        except ImportError:
            raise RDMALibraryNotFoundError(
                "cppyy is required but not installed. "
                "Install it with: pip install cppyy"
            )

        try:
            # Find the latest version of cuobjclient package
            # Package name format: cuobjclient-<version>
            pkg_name = None

            try:
                # Get list of all packages
                all_packages = subprocess.check_output(
                    ["pkg-config", "--list-all"], text=True, stderr=subprocess.STDOUT
                ).strip()

                # Find cuobjclient packages and extract versions
                cuobjclient_packages = []
                for line in all_packages.split("\n"):
                    if line.startswith("cuobjclient-"):
                        pkg_name_in_line = line.split()[0]
                        cuobjclient_packages.append(pkg_name_in_line)

                if cuobjclient_packages:
                    # Sort by version (latest first) - simple string sort works for most versions
                    cuobjclient_packages.sort(reverse=True)
                    pkg_name = cuobjclient_packages[0]
                    logger.debug(f"Found cuobjclient packages: {cuobjclient_packages}")
                    logger.debug(f"Selected package: {pkg_name}")

            except subprocess.CalledProcessError as e:
                logger.debug(f"pkg-config --list-all failed: {e}")

            # Final check
            if pkg_name is None:
                raise RDMALibraryNotFoundError(
                    "cuobjclient-* package not found via pkg-config. "
                    "Make sure cuobjclient is installed and PKG_CONFIG_PATH is set correctly. "
                    "Expected package name format: cuobjclient-<version>"
                )

            # Get include path
            include_path = subprocess.check_output(
                ["pkg-config", "--cflags-only-I", pkg_name], text=True
            ).strip()

            # Get library paths
            lib_paths = subprocess.check_output(
                ["pkg-config", "--libs-only-L", pkg_name], text=True
            ).strip()

            # Get library names
            lib_names = subprocess.check_output(
                ["pkg-config", "--libs-only-l", pkg_name], text=True
            ).strip()

            # Get all libs flags (for logging)
            libs = subprocess.check_output(
                ["pkg-config", "--libs", pkg_name], text=True
            ).strip()

            logger.info(f"Using pkg-config package: {pkg_name}")
            logger.debug(
                f"Found cuobjclient: include={include_path}, lib_paths={lib_paths}, lib_names={lib_names}"
            )

        except subprocess.CalledProcessError as e:
            raise RDMALibraryNotFoundError(
                "cuobjclient not found via pkgconfig. "
                "Make sure it's installed and PKG_CONFIG_PATH is set correctly."
            ) from e
        except FileNotFoundError:
            raise RDMALibraryNotFoundError(
                "pkg-config not found. Install it with your package manager."
            )

        try:
            # Add include path to cppyy
            if include_path:
                # Parse include paths (can be multiple: -I/path1 -I/path2)
                for path in include_path.split():
                    if path.startswith("-I"):
                        clean_path = path[2:].strip()
                        if clean_path:
                            cppyy.add_include_path(clean_path)
                            logger.debug(f"Added include path: {clean_path}")

            # Add library paths to cppyy
            if lib_paths:
                # Parse library paths: -L/path1 -L/path2
                for flag in lib_paths.split():
                    if flag.startswith("-L"):
                        lib_path = flag[2:].strip()
                        if lib_path:
                            cppyy.add_library_path(lib_path)
                            logger.debug(f"Added library path: {lib_path}")

            # Include the header file
            logger.debug("Including cuobjclient.h")
            cppyy.include("cuobjclient.h")

            # Load the library
            logger.debug("Loading cuobjclient library")
            cppyy.load_library("cuobjclient")

            # Return the cppyy.gbl module for later use
            logger.info("cuobjclient library loaded successfully")
            return cppyy.gbl

        except Exception as e:
            raise RDMAConnectionError(f"Failed to load cuobjclient library: {e}") from e

    def _register_event_handlers(self):
        """Register event handlers on the S3 client's event system."""
        event_system = self.client.meta.events

        # Register before-sign handlers to add RDMA tokens
        # Use before-sign (not before-send) so the x-amz-rdma-token header
        # is included in the AWS signature calculation.
        # Use register_first to ensure our handler runs before any user-registered
        # handlers that might try to modify or test the RDMA token header.
        event_system.register_first(
            "before-sign.s3.GetObject",
            self._add_rdma_token,
            unique_id="rdma-token-GetObject",
        )
        event_system.register_first(
            "before-sign.s3.PutObject",
            self._add_rdma_token,
            unique_id="rdma-token-PutObject",
        )
        event_system.register_first(
            "before-sign.s3.UploadPart",
            self._add_rdma_token,
            unique_id="rdma-token-UploadPart",
        )

        # Remove the "Expect: 100-continue" header botocore adds for PUT/POST
        # requests (see botocore.handlers.add_expect_header). RDMA-enabled
        # PutObject/UploadPart requests carry no HTTP body (the real payload
        # transfers out-of-band via RDMA), so there is nothing for the server
        # to "continue" for; leaving the header in place can cause the server
        # to stall waiting for a body that will never be sent over HTTP.
        event_system.register(
            "before-sign.s3.PutObject",
            self._remove_expect_header,
            unique_id="rdma-remove-expect-PutObject",
        )
        event_system.register(
            "before-sign.s3.UploadPart",
            self._remove_expect_header,
            unique_id="rdma-remove-expect-UploadPart",
        )

        # Register cleanup handlers
        event_system.register("after-call.s3", self._cleanup_descriptor)
        event_system.register("after-call-error.s3", self._cleanup_descriptor)

        logger.debug("Event handlers registered")

    def _patch_methods(self):
        """Monkey-patch S3 client methods to accept buffer parameter."""
        # Save original methods
        self._orig_get_object = self.client.get_object
        self._orig_put_object = self.client.put_object
        self._orig_upload_part = self.client.upload_part

        # Replace with wrapped versions (using @wraps decorator)
        self.client.get_object = self._create_get_object_wrapper()
        self.client.put_object = self._create_put_object_wrapper()
        self.client.upload_part = self._create_upload_part_wrapper()

        logger.debug("S3 client methods patched")

    def _create_get_object_wrapper(self):
        """Create a wrapped get_object method with proper decoration."""

        @wraps(self._orig_get_object)
        def get_object_wrapper(**kwargs):
            """
            Wrapper for get_object that accepts 'buffer' and 'is_buffer_registered' parameters.

            Args:
                **kwargs: Standard get_object parameters plus optional 'buffer' and 'is_buffer_registered'

            Returns:
                Standard get_object response

            Raises:
                RDMANotSupportedError: If server returns 501
            """
            buffer = kwargs.pop("buffer", None)
            is_buffer_registered = kwargs.pop("is_buffer_registered", False)

            if buffer is not None:
                logger.debug(
                    f"get_object with RDMA buffer, type={type(buffer).__name__}, is_registered={is_buffer_registered}"
                )

                # Store in context for event handlers
                buffer_ctx.set(
                    {
                        "buffer": buffer,
                        "operation": "GET",
                        "is_buffer_registered": is_buffer_registered,
                    }
                )

            try:
                return self._orig_get_object(**kwargs)

            except ClientError as e:
                # Check if this is a 501 (RDMA not supported) error
                if buffer is not None:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    status_code = e.response.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode", 0
                    )

                    if error_code == "501" or status_code == 501:
                        raise RDMANotSupportedError(
                            "RDMA not supported by server. "
                            "Retry without 'buffer' parameter for normal S3 transfer."
                        ) from e
                raise
            finally:
                if buffer is not None:
                    buffer_ctx.set(None)

        return get_object_wrapper

    def _create_put_object_wrapper(self):
        """Create a wrapped put_object method with proper decoration."""

        @wraps(self._orig_put_object)
        def put_object_wrapper(**kwargs):
            """
            Wrapper for put_object that accepts 'buffer' and 'is_buffer_registered' parameters.

            Args:
                **kwargs: Standard put_object parameters plus optional 'buffer' and 'is_buffer_registered'

            Returns:
                Standard put_object response

            Raises:
                RDMANotSupportedError: If server returns 501
            """
            buffer = kwargs.pop("buffer", None)
            is_buffer_registered = kwargs.pop("is_buffer_registered", False)

            if buffer is not None:
                logger.debug(
                    f"put_object with RDMA buffer, type={type(buffer).__name__}, is_registered={is_buffer_registered}"
                )

                buffer_ctx.set(
                    {
                        "buffer": buffer,
                        "operation": "PUT",
                        "is_buffer_registered": is_buffer_registered,
                    }
                )

                # For RDMA PUT: send empty body with ContentLength=0
                kwargs["Body"] = b""
                kwargs["ContentLength"] = 0

            try:
                return self._orig_put_object(**kwargs)

            except ClientError as e:
                if buffer is not None:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    status_code = e.response.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode", 0
                    )

                    if error_code == "501" or status_code == 501:
                        raise RDMANotSupportedError(
                            "RDMA not supported by server. "
                            "Retry without 'buffer' parameter for normal S3 transfer."
                        ) from e
                raise
            finally:
                if buffer is not None:
                    buffer_ctx.set(None)

        return put_object_wrapper

    def _create_upload_part_wrapper(self):
        """Create a wrapped upload_part method with proper decoration."""

        @wraps(self._orig_upload_part)
        def upload_part_wrapper(**kwargs):
            """
            Wrapper for upload_part that accepts 'buffer' and 'is_buffer_registered' parameters.

            Args:
                **kwargs: Standard upload_part parameters plus optional 'buffer' and 'is_buffer_registered'

            Returns:
                Standard upload_part response

            Raises:
                RDMANotSupportedError: If server returns 501
            """
            buffer = kwargs.pop("buffer", None)
            is_buffer_registered = kwargs.pop("is_buffer_registered", False)

            if buffer is not None:
                logger.debug(
                    f"upload_part with RDMA buffer, type={type(buffer).__name__}, is_registered={is_buffer_registered}"
                )

                buffer_ctx.set(
                    {
                        "buffer": buffer,
                        "operation": "PUT",
                        "is_buffer_registered": is_buffer_registered,
                    }
                )

                # For RDMA upload_part: send empty body with ContentLength=0
                kwargs["Body"] = b""
                kwargs["ContentLength"] = 0

            try:
                return self._orig_upload_part(**kwargs)

            except ClientError as e:
                if buffer is not None:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    status_code = e.response.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode", 0
                    )

                    if error_code == "501" or status_code == 501:
                        raise RDMANotSupportedError(
                            "RDMA not supported by server. "
                            "Retry without 'buffer' parameter for normal S3 transfer."
                        ) from e
                raise
            finally:
                if buffer is not None:
                    buffer_ctx.set(None)

        return upload_part_wrapper

    def _add_rdma_token(self, request, **kwargs):
        """
        Event handler for 'before-sign' event.
        Adds x-amz-rdma-token header to the request before AWS signature calculation.

        This handler is called after the request object is created but before
        the request is signed, ensuring that the RDMA token header is included
        in the AWS signature.

        Args:
            request: AWSRequest object (from before-sign event)
            **kwargs: Additional event parameters (operation_name, signing_name, etc.)

        Raises:
            RDMATokenError: If token generation fails
        """
        ctx = buffer_ctx.get()
        if ctx and ctx.get("buffer") is not None:
            buffer = ctx["buffer"]
            operation = ctx["operation"]
            is_buffer_registered = ctx.get("is_buffer_registered", False)

            try:
                # Get buffer information using helper method
                buffer_ptr, size, buffer_type = self._get_buffer_info(buffer)

                logger.debug(
                    f"Buffer: type={buffer_type}, size={size}, operation={operation}, is_buffer_registered={is_buffer_registered}"
                )

                # Step 1: Register buffer and get descriptor (if not already registered)
                #
                # NOTE: "before-sign" fires once per HTTP attempt, so on retries this
                # handler can run multiple times for the same logical operation call,
                # while "after-call"/"after-call-error" (which free the descriptor via
                # _cleanup_descriptor) only fire once at the very end of the retry
                # sequence. If a previous attempt already registered the descriptor
                # (tracked via ctx["buffer_ptr"]), we must not call
                # cuMemObjGetDescriptor again for the same buffer, since it can only
                # be registered once.
                already_registered_this_call = "buffer_ptr" in ctx
                if not is_buffer_registered and not already_registered_this_call:
                    # Call: cuMemObjGetDescriptor(buffer, size)
                    err = self.cuobjclient.cuMemObjGetDescriptor(buffer_ptr, size)
                    if err != self.gbl.CU_OBJ_SUCCESS:
                        raise RDMATokenError(
                            f"cuMemObjGetDescriptor failed with error code {err}"
                        )
                    logger.debug(f"Buffer descriptor registered successfully")
                else:
                    logger.debug(
                        "Buffer already registered (pre-registered or prior retry "
                        "attempt), skipping cuMemObjGetDescriptor"
                    )

                # Step 2: Get RDMA token
                # Call: cuMemObjGetRDMAToken(buffer, size, offset, mode, &desc_str)
                # mode: CUOBJ_PUT for upload, CUOBJ_GET for download
                mode = self.gbl.CUOBJ_PUT if operation == "PUT" else self.gbl.CUOBJ_GET
                offset = 0

                # Create char** using ctypes
                # cuMemObjGetRDMAToken expects char** (pointer to char*)
                char_ptr = ctypes.POINTER(ctypes.c_char)()
                char_ptr_ptr = ctypes.pointer(char_ptr)

                err = self.cuobjclient.cuMemObjGetRDMAToken(
                    buffer_ptr, size, offset, mode, char_ptr_ptr
                )

                if err != self.gbl.CU_OBJ_SUCCESS:
                    # Clean up descriptor on failure, but only if we just registered
                    # it during *this* attempt. If it was already registered by the
                    # caller, or by a prior retry attempt of this same call, leave it
                    # in place (a later retry attempt or the final after-call/
                    # after-call-error handler will own freeing it).
                    if not is_buffer_registered and not already_registered_this_call:
                        self.cuobjclient.cuMemObjPutDescriptor(buffer_ptr)
                    raise RDMATokenError(
                        f"cuMemObjGetRDMAToken failed with error code {err}"
                    )

                # Convert char* to Python string
                # char_ptr_ptr.contents gives us the char* (POINTER(c_char))
                # Cast to c_char_p to get null-terminated string, then get .value
                if char_ptr_ptr.contents:
                    # Cast POINTER(c_char) to c_char_p to access the string value
                    c_str = ctypes.cast(char_ptr_ptr.contents, ctypes.c_char_p)
                    token = c_str.value.decode("utf-8") if c_str.value else ""
                else:
                    c_str = None
                    token = ""

                if not token:
                    # Clean up
                    if c_str:
                        self.cuobjclient.cuMemObjPutRDMAToken(c_str)
                    if not is_buffer_registered and not already_registered_this_call:
                        self.cuobjclient.cuMemObjPutDescriptor(buffer_ptr)
                    raise RDMATokenError("cuMemObjGetRDMAToken returned empty token")

                logger.debug(f"RDMA token generated (length={len(token)})")

                # Store info for cleanup
                ctx["buffer_ptr"] = buffer_ptr
                ctx["char_ptr"] = c_str  # Store the c_char_p for cleanup
                buffer_ctx.set(ctx)

                # Add token to HTTP headers
                request.headers["x-amz-rdma-token"] = token
                logger.debug(f"Added x-amz-rdma-token header")

                # Free token immediately (server will use the string value)
                # cuMemObjPutRDMAToken expects char* - use the c_char_p we created
                # NOTE:
                # In the new release (13.4) of cuobjclient, cuMemGetRDMAToken will increase QP ref count,
                # so we need to call cuMemObjPutRDMAToken only after the RDMA operation is done.
                # However, this is disabled by default, unless we set it in cuobj.json:
                # "properties": {
                #        "allow_rdma_token_reset": true
                # },
                # so it's ok to call cuMemObjPutRDMAToken after we copy the string value.
                self.cuobjclient.cuMemObjPutRDMAToken(c_str)
                logger.debug("Token string freed")

            except RDMATokenError:
                raise
            except Exception as e:
                # Clean up descriptor on failure if we registered it
                if not is_buffer_registered and "buffer_ptr" in ctx:
                    try:
                        self.cuobjclient.cuMemObjPutDescriptor(ctx["buffer_ptr"])
                    except Exception as cleanup_error:
                        logger.error(f"Failed to cleanup descriptor: {cleanup_error}")

                raise RDMATokenError(f"RDMA token generation failed: {e}") from e

    def _remove_expect_header(self, request, **kwargs):
        """
        Event handler for 'before-sign' event on PutObject/UploadPart.

        Removes the "Expect: 100-continue" header (added by botocore's
        add_expect_header on before-call) for RDMA-enabled requests, since
        those requests carry no HTTP body for the server to acknowledge -
        the actual payload is transferred out-of-band via RDMA.

        Args:
            request: AWSRequest object (from before-sign event)
            **kwargs: Additional event parameters (operation_name, signing_name, etc.)
        """
        ctx = buffer_ctx.get()
        if ctx and ctx.get("buffer") is not None:
            if "Expect" in request.headers:
                del request.headers["Expect"]
                logger.debug(
                    "Removed Expect header from RDMA-enabled request"
                )

    def _cleanup_descriptor(self, **kwargs):
        """
        Event handler for 'after-call' and 'after-call-error' events.
        Frees the RDMA descriptor after response (or error).

        Args:
            **kwargs: Event parameters (varies by event type)
        """
        ctx = buffer_ctx.get()
        if ctx and "buffer_ptr" in ctx:
            try:
                buffer_ptr = ctx["buffer_ptr"]
                is_buffer_registered = ctx.get("is_buffer_registered", False)

                logger.debug(
                    f"Cleaning up RDMA descriptor for buffer (is_registered={is_buffer_registered})"
                )

                # Free the descriptor (only if we registered it)
                # Call: cuMemObjPutDescriptor(buffer)
                if not is_buffer_registered:
                    self.cuobjclient.cuMemObjPutDescriptor(buffer_ptr)
                    logger.debug("Descriptor freed")
                else:
                    logger.debug(
                        "Buffer was pre-registered, skipping cuMemObjPutDescriptor"
                    )

                del ctx["buffer_ptr"]
                if "char_ptr" in ctx:
                    del ctx["char_ptr"]

                logger.debug("Descriptor cleanup successful")
            except Exception as e:
                # Log warning but don't fail the request
                warnings.warn(f"Failed to free RDMA descriptor: {e}", RuntimeWarning)
                logger.error(f"Descriptor cleanup failed: {e}")
