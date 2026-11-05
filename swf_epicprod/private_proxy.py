"""A private copy of a grid proxy, for XRootD clients.

XRootD's TLS and GSI refuse a proxy file that others can read ("has excessive
access rights"). The service proxy the agent shares,
``/etc/swf-monitor/longproxy-for-rucio``, is group-readable (mode 0640), so a
caller hands xrdcp a mode-0600 copy that lasts for its block.
"""
import contextlib
import tempfile
from pathlib import Path


@contextlib.contextmanager
def private_copy(proxy, dir=None, prefix='proxy-'):
    """Yield the path of a mode-0600 copy of ``proxy``; removed when the block ends."""
    with tempfile.TemporaryDirectory(prefix=prefix, dir=dir) as temp:
        private = Path(temp) / 'proxy'
        private.touch(mode=0o600)
        private.write_bytes(Path(proxy).read_bytes())
        yield str(private)
