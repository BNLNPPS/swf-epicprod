"""Campaign assessment harness — production-side epicprod code
(docs/EPICPROD_ASSESSMENTS_V1.md).

corun-ai runs the invariant piece — the model — as a generic job; this
package is everything around it: the evidence bundle and submission
front end (``trigger``, ``bundle``), the enforcement end on the
completion callback (``enforce``), the corun configuration bootstrap
(``bootstrap``), and the shared content — templates, artifact schema,
validation (``spec``).
"""
