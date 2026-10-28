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
Exception classes for boto3_infinia.rdma
"""


class RDMAPluginError(Exception):
    """Base exception for all RDMA plugin errors"""

    pass


class RDMANotSupportedError(RDMAPluginError):
    """Raised when RDMA is not supported by the server (HTTP 501)"""

    pass


class RDMAConnectionError(RDMAPluginError):
    """Raised when cuObjClient connection fails"""

    pass


class RDMATokenError(RDMAPluginError):
    """Raised when RDMA token generation or management fails"""

    pass


class RDMALibraryNotFoundError(RDMAPluginError):
    """Raised when libcuobjclient library is not found"""

    pass
