#!/usr/bin/env python3
"""Thin wrapper — the assessment trigger lives in the package:
python -m swf_epicprod.assessment.trigger. See
docs/EPICPROD_ASSESSMENTS_V1.md."""

import sys

from swf_epicprod.assessment.trigger import main

if __name__ == '__main__':
    sys.exit(main())
