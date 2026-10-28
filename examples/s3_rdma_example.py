#!/usr/bin/env python3
"""
Advanced RDMA Example for boto3_infinia.rdma

This example demonstrates advanced features:
1. Simple upload and download with RDMA (NumPy)
2. Multipart upload with RDMA
3. GPU memory support with CuPy (zero-copy GPU↔Network)
4. Buffer pre-registration for performance optimization
5. Context manager for automatic buffer registration/cleanup
6. Mixed multipart upload (bytearray + NumPy + CuPy, with/without RDMA)
7. Performance metrics and throughput calculation
8. RDMA verification via response headers:
   - x-amz-rdma-reply: Status code (200/206 for success)
   - x-amz-rdma-bytes-transferred: Number of bytes transferred via RDMA
9. Response body verification (should be empty for RDMA downloads)
10. Error handling and fallback

Prerequisites:
1. Install: pip install boto3 numpy
2. Optional GPU: pip install cupy-cuda11x  (or cupy-cuda12x for CUDA 12)
3. Install cuobjclient library
4. S3 server with RDMA support

Usage:
  # Using environment variable (recommended)
  export AWS_ENDPOINT_URL_S3=http://localhost:9000
  python s3_rdma_example.py --bucket my-bucket --size-mb 10

  # Or specify endpoint via command line
  python s3_rdma_example.py --bucket my-bucket --endpoint-url http://localhost:9000 --size-mb 10

  # With multipart upload
  python s3_rdma_example.py --bucket my-bucket --multipart

  # With GPU example (requires CuPy)
  python s3_rdma_example.py --bucket my-bucket --gpu --size-mb 100

  # With buffer pre-registration (performance optimization)
  python s3_rdma_example.py --bucket my-bucket --register-buffer

  # With context manager for automatic cleanup
  python s3_rdma_example.py --bucket my-bucket --context-manager

  # Mixed multipart upload (bytearray + NumPy + CuPy)
  python s3_rdma_example.py --bucket my-bucket --mixed-multipart

  # All examples
  python s3_rdma_example.py --bucket my-bucket --multipart --gpu --register-buffer --context-manager --mixed-multipart --verbose
"""

import argparse
import logging
import sys
import time

import boto3
import numpy as np

# Try to import CuPy for GPU examples
try:
    import cupy as cp

    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None

# Import the RDMA plugin
try:
    from boto3_infinia.rdma import Boto3RdmaPlugin, RDMANotSupportedError
except ImportError:
    # If running from source
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
    from boto3_infinia.rdma import Boto3RdmaPlugin, RDMANotSupportedError


def setup_logging(verbose=False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")
    return logging.getLogger(__name__)


def check_rdma_response(response, operation, logger, expected_size=None):
    """
    Check response status and RDMA headers to verify RDMA was used.
    Also validates that response body is empty (RDMA protocol requirement).

    Args:
        response: boto3 response dict
        operation: string describing the operation (e.g., "upload", "download")
        logger: logger instance
        expected_size: expected number of bytes transferred (optional)

    Returns:
        bool: True if RDMA was confirmed, False otherwise
    """
    # Get response metadata
    metadata = response.get("ResponseMetadata", {})
    http_status = metadata.get("HTTPStatusCode")
    headers = metadata.get("HTTPHeaders", {})

    # Check for RDMA reply header
    rdma_reply = headers.get("x-amz-rdma-reply")
    rdma_bytes = headers.get("x-amz-rdma-bytes-transferred")

    logger.info(f"   HTTP Status: {http_status}")

    if rdma_reply:
        logger.info(f"   x-amz-rdma-reply: {rdma_reply}")

        # Check for successful RDMA status (200 or 206 for partial content)
        if rdma_reply in ("200", "206"):
            logger.info(f"   ✅ RDMA {operation} CONFIRMED!")

            # Check bytes transferred header (only present for GET/download operations)
            if rdma_bytes:
                bytes_transferred = int(rdma_bytes)
                logger.info(f"   x-amz-rdma-bytes-transferred: {bytes_transferred:,} bytes")

                # Verify against expected size if provided
                if expected_size is not None:
                    if bytes_transferred == expected_size:
                        logger.info(f"   ✅ Bytes transferred matches expected size")
                    else:
                        logger.warning(
                            f"   ⚠️  Bytes transferred ({bytes_transferred:,}) "
                            f"does not match expected size ({expected_size:,})"
                        )
            elif "download" in operation.lower() or "get" in operation.lower():
                # Only warn if this is a GET/download operation
                logger.warning(
                    f"   ⚠️  No x-amz-rdma-bytes-transferred header (expected for downloads)"
                )

            # Verify RDMA protocol: response body must be empty for both GET and PUT
            # (data transferred via RDMA, not HTTP body)

            # Check Content-Length header
            content_length = headers.get("content-length")
            if content_length is not None:
                content_length_int = int(content_length)
                if content_length_int == 0:
                    logger.info("   ✅ Content-Length: 0 (as expected for RDMA)")
                else:
                    logger.warning(
                        f"   ⚠️  Content-Length: {content_length_int} (expected 0 for RDMA)"
                    )

            # Check response body is actually empty
            if "Body" in response:
                body_content = response["Body"].read()
                if len(body_content) == 0:
                    logger.info("   ✅ Response body is empty (data transferred via RDMA)")
                else:
                    logger.warning(
                        f"   ⚠️  Response body not empty ({len(body_content)} bytes) - "
                        f"may indicate HTTP fallback"
                    )

            return True
        else:
            logger.warning(f"   ⚠️  RDMA {operation} returned status: {rdma_reply}")
            return False
    else:
        logger.warning(
            f"   ⚠️  No x-amz-rdma-reply header (RDMA may not be enabled on server)"
        )
        return False


def upload_with_rdma(s3, bucket, key, size_mb, logger):
    """Upload data using RDMA."""
    logger.info(f"=== Upload {size_mb}MB with RDMA ===")

    # Create random data
    size_bytes = size_mb * 1024 * 1024
    data = np.random.randint(0, 256, size=size_bytes, dtype=np.uint8)

    logger.info(f"Uploading to s3://{bucket}/{key}...")
    logger.info(f"   Note: For RDMA PUT, the plugin sets Body=b'' and ContentLength=0")
    logger.info(f"   (Actual data transfer happens via RDMA, not HTTP body)")
    start_time = time.time()

    try:
        response = s3.put_object(
            Bucket=bucket,
            Key=key,
            buffer=data,  # RDMA-enabled upload (no Body parameter!)
        )

        elapsed = time.time() - start_time
        throughput = size_mb / elapsed if elapsed > 0 else 0

        logger.info(f"✅ Upload completed in {elapsed:.2f}s ({throughput:.2f} MB/s)")
        logger.info(f"   ETag: {response.get('ETag')}")

        # Verify RDMA was used (includes response body validation)
        check_rdma_response(response, "upload", logger, expected_size=size_bytes)

        return data

    except RDMANotSupportedError:
        logger.warning("⚠️  RDMA not supported, falling back to normal upload")
        response = s3.put_object(Bucket=bucket, Key=key, Body=data.tobytes())
        elapsed = time.time() - start_time
        logger.info(f"✅ Normal upload completed in {elapsed:.2f}s")
        return data


def download_with_rdma(s3, bucket, key, expected_data, logger):
    """Download data using RDMA."""
    logger.info(f"\n=== Download from {key} with RDMA ===")

    # Get object size
    head = s3.head_object(Bucket=bucket, Key=key)
    size_bytes = head["ContentLength"]
    size_mb = size_bytes / (1024 * 1024)

    # Allocate buffer
    buffer = np.zeros(size_bytes, dtype=np.uint8)
    logger.info(f"Allocated {size_mb:.2f}MB buffer")

    start_time = time.time()

    try:
        response = s3.get_object(
            Bucket=bucket,
            Key=key,
            buffer=buffer,  # RDMA-enabled download
        )

        elapsed = time.time() - start_time
        throughput = size_mb / elapsed if elapsed > 0 else 0

        logger.info(f"✅ Download completed in {elapsed:.2f}s ({throughput:.2f} MB/s)")

        # Verify RDMA was used (includes response body validation)
        check_rdma_response(response, "download", logger, expected_size=size_bytes)

        # Verify data
        if expected_data is not None and np.array_equal(buffer, expected_data):
            logger.info("✅ Data verification PASSED")

        return buffer

    except RDMANotSupportedError:
        logger.warning("⚠️  RDMA not supported, falling back to normal download")
        response = s3.get_object(Bucket=bucket, Key=key)
        data = response["Body"].read()
        buffer[:] = np.frombuffer(data, dtype=np.uint8)
        elapsed = time.time() - start_time
        logger.info(f"✅ Normal download completed in {elapsed:.2f}s")
        return buffer


def multipart_upload_with_rdma(s3, bucket, key, part_size_mb, num_parts, logger):
    """Multipart upload using RDMA."""
    logger.info(f"\n=== Multipart Upload ({num_parts} parts x {part_size_mb}MB) ===")

    # Create multipart upload
    mpu = s3.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = mpu["UploadId"]
    logger.info(f"Created upload ID: {upload_id}")

    parts = []
    all_data = []
    start_time = time.time()

    try:
        for part_num in range(1, num_parts + 1):
            # Create part data
            part_size_bytes = part_size_mb * 1024 * 1024
            part_data = np.random.randint(0, 256, size=part_size_bytes, dtype=np.uint8)
            all_data.append(part_data)

            logger.info(f"Uploading part {part_num}/{num_parts} (RDMA: Body=b'', ContentLength=0)...")

            try:
                response = s3.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_num,
                    buffer=part_data,  # RDMA-enabled upload (no Body!)
                )

                parts.append({"PartNumber": part_num, "ETag": response["ETag"]})
                logger.info(f"✅ Part {part_num} uploaded (ETag: {response['ETag']})")

                # Verify RDMA was used for this part
                check_rdma_response(response, f"upload part {part_num}", logger, expected_size=part_size_bytes)

            except RDMANotSupportedError:
                logger.warning(
                    f"⚠️  RDMA not supported for part {part_num}, using fallback"
                )
                response = s3.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_num,
                    Body=part_data.tobytes(),
                )
                parts.append({"PartNumber": part_num, "ETag": response["ETag"]})

        # Complete multipart upload
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
        )

        elapsed = time.time() - start_time
        total_mb = part_size_mb * num_parts
        throughput = total_mb / elapsed if elapsed > 0 else 0

        logger.info(
            f"✅ Multipart upload completed in {elapsed:.2f}s ({throughput:.2f} MB/s)"
        )

        return np.concatenate(all_data)

    except Exception as e:
        logger.error(f"❌ Multipart upload failed: {e}")
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise


def gpu_upload_download_with_rdma(s3, bucket, key, size_mb, logger):
    """GPU upload and download using RDMA with CuPy (zero-copy!)."""
    if not CUPY_AVAILABLE:
        logger.info("\n=== GPU Example (CuPy) ===")
        logger.warning("⚠️  CuPy not available. Install with: pip install cupy")
        logger.info("(Skipping GPU examples)\n")
        return

    logger.info("\n=== GPU Upload/Download with RDMA (CuPy - Zero-Copy!) ===")
    logger.info("Zero-copy: GPU ↔ Network direct via RDMA (no CPU copy)")

    # Create GPU data
    size_bytes = size_mb * 1024 * 1024
    logger.info(f"Creating {size_mb}MB GPU array...")

    try:
        gpu_data = cp.random.randint(0, 256, size=size_bytes, dtype=cp.uint8)
        logger.info(f"✅ GPU array created: {gpu_data.shape}, dtype: {gpu_data.dtype}")
        logger.info(f"   Memory: GPU (device {gpu_data.device.id})")
    except Exception as e:
        logger.error(f"❌ Failed to create GPU array: {e}")
        logger.warning("Make sure CUDA is available and working")
        return

    # Upload from GPU (zero-copy: GPU → Network)
    logger.info(f"\nUploading from GPU to s3://{bucket}/{key}...")
    logger.info("(Zero-copy: Data goes directly from GPU memory to network)")
    logger.info(f"   Note: Plugin sets Body=b'' and ContentLength=0 for RDMA")
    start_time = time.time()

    try:
        response = s3.put_object(
            Bucket=bucket,
            Key=key,
            buffer=gpu_data,  # RDMA from GPU (no Body parameter!)
        )

        elapsed = time.time() - start_time
        throughput = size_mb / elapsed if elapsed > 0 else 0

        logger.info(
            f"✅ GPU upload completed in {elapsed:.2f}s ({throughput:.2f} MB/s)"
        )
        logger.info(f"   ETag: {response.get('ETag')}")

        # Verify RDMA was used (includes response body validation)
        size_bytes = size_mb * 1024 * 1024
        check_rdma_response(response, "GPU upload", logger, expected_size=size_bytes)

    except RDMANotSupportedError:
        logger.warning("⚠️  RDMA not supported, falling back (requires GPU→CPU copy)")
        # Fallback requires CPU copy
        cpu_data = cp.asnumpy(gpu_data)
        response = s3.put_object(Bucket=bucket, Key=key, Body=cpu_data.tobytes())
        elapsed = time.time() - start_time
        logger.info(f"✅ Fallback upload completed in {elapsed:.2f}s")

    # Download to GPU (zero-copy: Network → GPU)
    logger.info(f"\nDownloading from s3://{bucket}/{key} to GPU...")
    logger.info("(Zero-copy: Data goes directly from network to GPU memory)")

    # Allocate GPU buffer
    gpu_buffer = cp.zeros(size_bytes, dtype=cp.uint8)
    logger.info(f"Allocated GPU buffer: {gpu_buffer.shape}")

    start_time = time.time()

    try:
        response = s3.get_object(
            Bucket=bucket,
            Key=key,
            buffer=gpu_buffer,  # RDMA to GPU!
        )

        elapsed = time.time() - start_time
        throughput = size_mb / elapsed if elapsed > 0 else 0

        logger.info(
            f"✅ GPU download completed in {elapsed:.2f}s ({throughput:.2f} MB/s)"
        )

        # Verify RDMA was used (includes response body validation)
        size_bytes = size_mb * 1024 * 1024
        check_rdma_response(response, "GPU download", logger, expected_size=size_bytes)

        # Verify data (on GPU)
        if cp.array_equal(gpu_data, gpu_buffer):
            logger.info("✅ GPU data verification PASSED")
        else:
            logger.warning("⚠️  GPU data verification FAILED")

    except RDMANotSupportedError:
        logger.warning("⚠️  RDMA not supported, falling back (requires CPU→GPU copy)")
        response = s3.get_object(Bucket=bucket, Key=key)
        cpu_data = np.frombuffer(response["Body"].read(), dtype=np.uint8)
        gpu_buffer = cp.asarray(cpu_data)
        elapsed = time.time() - start_time
        logger.info(f"✅ Fallback download completed in {elapsed:.2f}s")

    logger.info("\n💡 Performance note:")
    logger.info("   With RDMA: GPU ↔ Network (0 CPU copies)")
    logger.info("   Without RDMA: GPU ↔ CPU ↔ Network (2 CPU copies)")
    logger.info("   RDMA eliminates expensive GPU↔CPU memory transfers!")


def mixed_multipart_upload_example(s3, bucket, logger):
    """
    Mixed multipart upload example demonstrating different memory types and RDMA modes.

    Object size: 10MB + 1 byte
    - Part 1 (5MB): bytearray without RDMA
    - Part 2 (5MB): NumPy array with RDMA
    - Part 3 (1 byte): CuPy GPU array with RDMA (if available)

    Finally downloads and verifies the complete object without RDMA.
    """
    logger.info("\n=== Mixed Multipart Upload Example ===")
    logger.info("Uploading 10MB + 1 byte object in 3 parts:")
    logger.info("  Part 1: 5MB bytearray (no RDMA)")
    logger.info("  Part 2: 5MB NumPy (with RDMA)")
    logger.info(
        "  Part 3: 1 byte CuPy GPU (with RDMA)"
        if CUPY_AVAILABLE
        else "  Part 3: 1 byte bytearray (no RDMA, CuPy not available)"
    )

    key = "rdma-mixed-multipart.bin"
    part_size_mb = 5
    part_size_bytes = part_size_mb * 1024 * 1024

    # Create multipart upload
    mpu = s3.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = mpu["UploadId"]
    logger.info(f"\nCreated multipart upload ID: {upload_id}")

    parts = []
    all_data_parts = []

    try:
        # Part 1: 5MB bytearray without RDMA
        logger.info(f"\n--- Part 1/3: 5MB bytearray (NO RDMA) ---")
        part1_data = bytearray(
            np.random.randint(0, 256, size=part_size_bytes, dtype=np.uint8)
        )
        all_data_parts.append(bytes(part1_data))

        logger.info(f"  Uploading part 1 (bytearray, no RDMA)...")
        start_time = time.time()

        response = s3.upload_part(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=1,
            Body=part1_data,  # No buffer parameter = no RDMA
        )

        elapsed = time.time() - start_time
        parts.append({"PartNumber": 1, "ETag": response["ETag"]})
        logger.info(
            f"  ✅ Part 1 uploaded in {elapsed:.3f}s (ETag: {response['ETag']})"
        )
        logger.info(f"     Method: Standard upload (bytearray, no RDMA)")

        # Part 2: 5MB NumPy array with RDMA
        logger.info(f"\n--- Part 2/3: 5MB NumPy (WITH RDMA) ---")
        part2_data = np.random.randint(0, 256, size=part_size_bytes, dtype=np.uint8)
        all_data_parts.append(part2_data.tobytes())

        logger.info(f"  Uploading part 2 (NumPy with RDMA)...")
        start_time = time.time()

        try:
            response = s3.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=2,
                buffer=part2_data,  # buffer parameter = RDMA enabled
            )

            elapsed = time.time() - start_time
            parts.append({"PartNumber": 2, "ETag": response["ETag"]})
            logger.info(
                f"  ✅ Part 2 uploaded in {elapsed:.3f}s (ETag: {response['ETag']})"
            )
            logger.info(f"     Method: RDMA upload (NumPy CPU array)")

            # Check RDMA reply
            part2_size = part_size_mb * 1024 * 1024
            check_rdma_response(response, "upload part 2 (NumPy)", logger, expected_size=part2_size)

        except RDMANotSupportedError:
            logger.warning("  ⚠️  RDMA not supported for part 2, using fallback")
            response = s3.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=2,
                Body=part2_data.tobytes(),
            )
            parts.append({"PartNumber": 2, "ETag": response["ETag"]})
            elapsed = time.time() - start_time
            logger.info(f"  ✅ Part 2 uploaded in {elapsed:.3f}s (fallback)")

        # Part 3: 1 byte CuPy GPU array with RDMA (or bytearray if CuPy not available)
        logger.info(
            f"\n--- Part 3/3: 1 byte {'CuPy GPU (WITH RDMA)' if CUPY_AVAILABLE else 'bytearray (NO RDMA)'} ---"
        )

        if CUPY_AVAILABLE:
            # Use CuPy GPU array
            try:
                part3_data_gpu = cp.random.randint(0, 256, size=1, dtype=cp.uint8)
                part3_data_bytes = cp.asnumpy(part3_data_gpu).tobytes()
                all_data_parts.append(part3_data_bytes)

                logger.info(f"  Uploading part 3 (CuPy GPU with RDMA)...")
                logger.info(
                    f"     GPU array created on device {part3_data_gpu.device.id}"
                )
                start_time = time.time()

                try:
                    response = s3.upload_part(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=3,
                        buffer=part3_data_gpu,  # GPU buffer = RDMA from GPU!
                    )

                    elapsed = time.time() - start_time
                    parts.append({"PartNumber": 3, "ETag": response["ETag"]})
                    logger.info(
                        f"  ✅ Part 3 uploaded in {elapsed:.3f}s (ETag: {response['ETag']})"
                    )
                    logger.info(
                        f"     Method: RDMA upload (CuPy GPU → Network, zero-copy!)"
                    )

                    # Check RDMA reply
                    part3_size = 1  # Part 3 is only 1 byte
                    check_rdma_response(response, "upload part 3 (CuPy GPU)", logger, expected_size=part3_size)

                except RDMANotSupportedError:
                    logger.warning("  ⚠️  RDMA not supported for part 3, using fallback")
                    response = s3.upload_part(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=3,
                        Body=part3_data_bytes,
                    )
                    parts.append({"PartNumber": 3, "ETag": response["ETag"]})
                    elapsed = time.time() - start_time
                    logger.info(f"  ✅ Part 3 uploaded in {elapsed:.3f}s (fallback)")

            except Exception as e:
                logger.error(f"  ❌ Failed to create GPU array: {e}")
                logger.info(f"  Falling back to bytearray for part 3...")
                # Fall back to bytearray
                part3_data = bytearray([np.random.randint(0, 256)])
                all_data_parts.append(bytes(part3_data))
                response = s3.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=3,
                    Body=part3_data,
                )
                parts.append({"PartNumber": 3, "ETag": response["ETag"]})
                logger.info(f"  ✅ Part 3 uploaded (bytearray fallback)")
        else:
            # CuPy not available, use bytearray
            part3_data = bytearray([np.random.randint(0, 256)])
            all_data_parts.append(bytes(part3_data))

            logger.info(
                f"  Uploading part 3 (bytearray, no RDMA - CuPy not available)..."
            )
            start_time = time.time()

            response = s3.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=3,
                Body=part3_data,
            )

            elapsed = time.time() - start_time
            parts.append({"PartNumber": 3, "ETag": response["ETag"]})
            logger.info(
                f"  ✅ Part 3 uploaded in {elapsed:.3f}s (ETag: {response['ETag']})"
            )
            logger.info(f"     Method: Standard upload (bytearray, no RDMA)")

        # Complete multipart upload
        logger.info(f"\nCompleting multipart upload...")
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
        )
        logger.info(f"✅ Multipart upload completed!")

        # Verify: Download without RDMA and check integrity
        logger.info(f"\n--- Verification: Download without RDMA ---")
        logger.info(f"Downloading complete object to verify integrity...")

        response = s3.get_object(Bucket=bucket, Key=key)
        downloaded_data = response["Body"].read()

        expected_data = b"".join(all_data_parts)
        expected_size = len(expected_data)
        actual_size = len(downloaded_data)

        logger.info(f"  Expected size: {expected_size} bytes (10 MB + 1 byte)")
        logger.info(f"  Downloaded size: {actual_size} bytes")

        if downloaded_data == expected_data:
            logger.info(f"  ✅ DATA INTEGRITY VERIFIED! All bytes match.")
        else:
            logger.error(f"  ❌ DATA MISMATCH!")
            # Find first mismatch
            for i in range(min(len(downloaded_data), len(expected_data))):
                if downloaded_data[i] != expected_data[i]:
                    logger.error(
                        f"     First mismatch at byte {i}: expected {expected_data[i]}, got {downloaded_data[i]}"
                    )
                    break

        logger.info(f"\n💡 Summary:")
        logger.info(f"  Part 1: Standard upload (bytearray)")
        logger.info(f"  Part 2: RDMA upload (NumPy CPU array)")
        logger.info(
            f"  Part 3: {'RDMA upload (CuPy GPU array)' if CUPY_AVAILABLE else 'Standard upload (bytearray)'}"
        )
        logger.info(f"  Verification: Standard download (no RDMA)")
        logger.info(f"  Result: Mixed upload modes work correctly!")

    except Exception as e:
        logger.error(f"❌ Mixed multipart upload failed: {e}")
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise


def register_buffer_example(s3, plugin, bucket, logger):
    """
    Demonstrate pre-registering buffers for performance optimization.

    When you use the same buffer multiple times, pre-registering it can
    improve performance by skipping the buffer registration overhead on
    each operation.
    """
    logger.info("\n=== Buffer Pre-Registration Example ===")
    logger.info("Pre-registering a buffer for multiple operations...")

    # Create a long-lived buffer
    size_mb = 5
    size_bytes = size_mb * 1024 * 1024
    buffer = np.zeros(size_bytes, dtype=np.uint8)

    # Pre-register the buffer with the RDMA system
    logger.info(f"Pre-registering {size_mb}MB buffer...")
    try:
        plugin.register_buffer(buffer)
        logger.info("✅ Buffer registered successfully")
    except Exception as e:
        logger.error(f"❌ Failed to register buffer: {e}")
        return

    # Now use the buffer multiple times with is_buffer_registered=True
    # This skips the registration overhead on each operation
    logger.info(f"\nUsing pre-registered buffer for multiple operations...")

    num_iterations = 5
    start_time = time.time()

    for i in range(num_iterations):
        key = f"rdma-registered-{i}.bin"

        # Fill buffer with data
        buffer[:] = np.random.randint(0, 256, size=size_bytes, dtype=np.uint8)

        # Upload using pre-registered buffer
        try:
            logger.info(f"  Upload {i + 1}/{num_iterations}: {key}...")
            response = s3.put_object(
                Bucket=bucket,
                Key=key,
                buffer=buffer,
                is_buffer_registered=True,  # Skip re-registration!
            )
            logger.info(f"    ✅ Uploaded (ETag: {response.get('ETag')})")

            # Download using pre-registered buffer
            logger.info(f"  Download {i + 1}/{num_iterations}: {key}...")
            response = s3.get_object(
                Bucket=bucket,
                Key=key,
                buffer=buffer,
                is_buffer_registered=True,  # Skip re-registration!
            )
            logger.info(f"    ✅ Downloaded")

        except Exception as e:
            logger.error(f"    ❌ Operation {i + 1} failed: {e}")
            break

    elapsed = time.time() - start_time
    total_mb = size_mb * num_iterations * 2  # upload + download
    throughput = total_mb / elapsed if elapsed > 0 else 0

    logger.info(f"\n✅ Completed {num_iterations} upload+download cycles")
    logger.info(f"   Total time: {elapsed:.2f}s")
    logger.info(f"   Throughput: {throughput:.2f} MB/s")
    logger.info(f"   Average per operation: {elapsed / (num_iterations * 2):.3f}s")

    # Cleanup: Unregister the buffer when done
    logger.info(f"\nCleaning up pre-registered buffer...")
    try:
        plugin.unregister_buffer(buffer)
        logger.info("✅ Buffer unregistered successfully")
    except Exception as e:
        logger.warning(f"⚠️  Failed to unregister buffer: {e}")

    logger.info("\n💡 Performance tip:")
    logger.info("   Pre-registration is useful for long-lived buffers used")
    logger.info("   in multiple operations. It reduces per-operation overhead")
    logger.info("   by skipping the RDMA registration step each time.")


def context_manager_example(s3, plugin, bucket, logger):
    """
    Demonstrate using the context manager for automatic buffer cleanup.

    The context manager automatically registers the buffer on entry and
    unregisters it on exit, even if an exception occurs.
    """
    logger.info("\n=== Context Manager Example ===")
    logger.info("Using context manager for automatic buffer registration/cleanup...")

    # Create a buffer
    size_mb = 5
    size_bytes = size_mb * 1024 * 1024
    buffer = np.zeros(size_bytes, dtype=np.uint8)

    # Use context manager for automatic registration and cleanup
    try:
        with plugin.registered_buffer(buffer):
            logger.info(f"✅ Buffer automatically registered")
            logger.info(f"\nUsing buffer for multiple operations...")

            num_iterations = 3
            for i in range(num_iterations):
                key = f"rdma-context-{i}.bin"

                # Fill buffer with data
                buffer[:] = np.random.randint(0, 256, size=size_bytes, dtype=np.uint8)

                # Upload using pre-registered buffer
                logger.info(f"  Upload {i + 1}/{num_iterations}: {key}...")
                response = s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    buffer=buffer,
                    is_buffer_registered=True,  # Buffer is registered in context
                )
                logger.info(f"    ✅ Uploaded (ETag: {response.get('ETag')})")

                # Download using pre-registered buffer
                logger.info(f"  Download {i + 1}/{num_iterations}: {key}...")
                response = s3.get_object(
                    Bucket=bucket,
                    Key=key,
                    buffer=buffer,
                    is_buffer_registered=True,  # Buffer is registered in context
                )
                logger.info(f"    ✅ Downloaded")

        # Buffer is automatically unregistered here
        logger.info(f"\n✅ Buffer automatically unregistered on context exit")

    except Exception as e:
        logger.error(f"❌ Operation failed: {e}")
        logger.info("   (Buffer will still be automatically unregistered)")
        raise

    logger.info("\n💡 Context manager benefits:")
    logger.info("   - Automatic cleanup even if exceptions occur")
    logger.info("   - Cleaner code without explicit try/finally blocks")
    logger.info("   - Guaranteed resource deallocation")
    logger.info("\n💡 Usage pattern:")
    logger.info("   with plugin.registered_buffer(buffer):")
    logger.info("       # Use buffer with is_buffer_registered=True")
    logger.info("       s3.get_object(..., buffer=buffer, is_buffer_registered=True)")


def main():
    parser = argparse.ArgumentParser(
        description="Advanced RDMA examples for boto3_infinia.rdma",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument(
        "--endpoint-url",
        help="S3 endpoint URL (default: uses AWS_ENDPOINT_URL_S3 env var or AWS S3)",
    )
    parser.add_argument(
        "--size-mb", type=int, default=10, help="Size in MB for simple upload/download"
    )
    parser.add_argument(
        "--multipart", action="store_true", help="Run multipart upload example"
    )
    parser.add_argument("--gpu", action="store_true", help="Run GPU (CuPy) example")
    parser.add_argument(
        "--register-buffer",
        action="store_true",
        help="Run buffer pre-registration example",
    )
    parser.add_argument(
        "--context-manager",
        action="store_true",
        help="Run context manager example for automatic buffer cleanup",
    )
    parser.add_argument(
        "--mixed-multipart",
        action="store_true",
        help="Run mixed multipart upload example (bytearray + NumPy + CuPy)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    logger = setup_logging(args.verbose)

    try:
        # Create S3 client
        # Note: If --endpoint-url not specified, boto3 will use AWS_ENDPOINT_URL_S3 env var or default to AWS S3
        s3_kwargs = {}
        if args.endpoint_url:
            s3_kwargs["endpoint_url"] = args.endpoint_url

        # Disable SSL verification for local testing
        s3_kwargs["verify"] = False
        s3 = boto3.client("s3", **s3_kwargs)

        # Show CuPy availability
        if CUPY_AVAILABLE:
            logger.info(f"✅ CuPy available (GPU examples enabled)")
        else:
            logger.info("ℹ️  CuPy not available (GPU examples disabled)")
            logger.info("   Install with: pip install cupy-cuda11x (or cupy-cuda12x)")

        # Initialize RDMA plugin
        logger.info("\nInitializing RDMA plugin...")
        plugin = Boto3RdmaPlugin(s3)
        logger.info("✅ RDMA plugin initialized\n")

        # Example 1: Simple upload
        uploaded_data = upload_with_rdma(
            s3, args.bucket, "rdma-simple.bin", args.size_mb, logger
        )

        # Example 2: Simple download
        download_with_rdma(s3, args.bucket, "rdma-simple.bin", uploaded_data, logger)

        # Example 3: Multipart upload (optional)
        if args.multipart:
            multipart_data = multipart_upload_with_rdma(
                s3,
                args.bucket,
                "rdma-multipart.bin",
                part_size_mb=5,
                num_parts=3,
                logger=logger,
            )
            download_with_rdma(
                s3, args.bucket, "rdma-multipart.bin", multipart_data, logger
            )

        # Example 4: GPU upload/download (optional, requires CuPy)
        if args.gpu:
            gpu_upload_download_with_rdma(
                s3, args.bucket, "rdma-gpu.bin", args.size_mb, logger
            )

        # Example 5: Buffer pre-registration (optional)
        if args.register_buffer:
            register_buffer_example(s3, plugin, args.bucket, logger)

        # Example 6: Context manager for automatic cleanup (optional)
        if args.context_manager:
            context_manager_example(s3, plugin, args.bucket, logger)

        # Example 7: Mixed multipart upload (optional)
        if args.mixed_multipart:
            mixed_multipart_upload_example(s3, args.bucket, logger)

        logger.info("\n🎉 All examples completed successfully!")

    except Exception as e:
        logger.error(f"\n❌ Error: {e}", exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
