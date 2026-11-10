<!-- aperio:skill
name: backend-cms
version: 1.0.0
description: Backend and CMS guidance: API contracts, data models, authorization, validation, uploads, publishing workflow, and error handling.
scope: backend
depends_on:
conflicts:
provenance: aperio-builtin-v1
-->
Fix API contracts and data models before implementation; document request and response shapes plus error codes. Enforce authorization at the boundary for every operation, never in the client alone. Validate all input server-side, including uploads: type, size, and destination; keep media storage out of executable paths. Model the publishing workflow explicitly (draft, review, published) with allowed transitions. Return structured errors; do not leak stack traces or internal details. Never place secrets, credentials, or tokens in shareable state such as memory, briefs, or exported documents.
