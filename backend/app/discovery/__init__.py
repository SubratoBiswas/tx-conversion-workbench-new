"""Source-system discovery — connection probes and inventory scanners.

Per-source modules under this package share a small dispatch contract so
the connection layer and the discovery layer don't need to know about each
specific ERP. See ``connection_dispatch.py`` for the runtime interface and
``mock_netsuite.py`` / ``mock_ebs.py`` for v1 reference implementations.
"""
