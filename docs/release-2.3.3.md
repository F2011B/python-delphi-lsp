# Python Delphi LSP 2.3.3

Version 2.3.3 fixes cache daemon startup for large repositories.

- The daemon publishes its authenticated local endpoint before repository
  prewarming begins.
- Cache status and stop requests remain responsive while the index warms.
- Queries wait and retry during prewarming instead of failing or timing out on
  the daemon startup deadline.
- The idle timeout cannot terminate a daemon while its initial prewarm is
  still running.

This is a backwards-compatible patch release.
