# REST API Documentation

Every REST interface served by swf-monitor is described by an OpenAPI 3
schema generated from the code and published as interactive documentation.
This is the standard tooling pair for Django REST Framework services:
**drf-spectacular** generates the schema by introspecting the URL patterns,
viewsets, serializers, and endpoint docstrings; **Swagger UI** and **Redoc**
render it as browsable, interactive pages. The generated schema is the
machine-readable contract of each interface — clients can be generated from
it — while the design documents in this repository state the semantics and
the reasoning. Endpoint detail lives in one place, the schema; design
documents link to it rather than restating request and response shapes.

## Current state

- `drf-spectacular` is installed in swf-monitor (`requirements.txt`) and set
  as the DRF `DEFAULT_SCHEMA_CLASS`.
- The schema and both renderers serve on the internal face:
  `/swf-monitor/api/schema/` (OpenAPI YAML),
  `/swf-monitor/api/schema/swagger-ui/`, `/swf-monitor/api/schema/redoc/`.
- Generated coverage includes the PCS API and the validation interface v1
  endpoints ([EPICPROD_VALIDATION.md](EPICPROD_VALIDATION.md)), with
  endpoint docstrings serving as their descriptions.

## Plan

1. **External publication.** Route `/api/schema/` through the swf-remote
   proxy and admit it at the login wall (open read-only, like the
   validation v1 reads), so the documentation serves at
   `https://epic-devcloud.org/prod/api/schema/swagger-ui/` for
   collaborators outside the BNL perimeter.
2. **Base-URL index.** `GET /pcs/api/v1/` returns a small JSON index naming
   the interface's endpoints and the documentation URL, so the base URL
   handed to an integrating project lands somewhere useful instead of 404.
3. **Schema quality pass.** Schema metadata (title, version, description);
   tag grouping by surface (validation, pcs, panda, monitor);
   `@extend_schema` annotations on the validation v1 endpoints declaring
   their request and response bodies, so the generated documentation states
   the actual JSON shapes rather than empty schemas.
4. **Self-hosted UI assets.** The renderer pages currently load Swagger UI
   and Redoc assets from a public CDN in the browser. Installing
   `drf-spectacular[sidecar]` serves them from the application itself,
   removing the external dependence (requirements change; full deploy).
5. **WFMS documentation.** The system documentation
   (<https://epic-wfms-docs.readthedocs.io>, source `epic-wfms-docs/`) gains
   an **APIs** page: a summary of the public REST surfaces — what each
   serves and for whom — linking the live Swagger and Redoc pages and the
   interface design documents. The schema pages remain the single source of
   endpoint detail; the APIs page orients, it does not restate.

Steps 1–3 need only code and template changes (lightweight deploys on the
monitor side plus an swf-remote deploy); step 4 carries a requirements
change and rides the next full deploy.
