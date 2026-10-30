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
Boto3 extensions for DDN Infinia.

Features live in submodules and are imported explicitly so that optional
dependencies (e.g. cppyy for RDMA) are only needed by the features you use:

    from boto3_infinia.rdma import Boto3RdmaPlugin
    from boto3_infinia.tagging import Boto3TaggingPlugin
"""

__version__ = "2.5.1"
