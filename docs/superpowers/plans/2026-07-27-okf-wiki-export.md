# OKF Wiki Export Implementation Plan

1. Add failing tests for parser wiring, OKF structure, complete symbol export,
   link validity, filename collisions, deterministic replacement, and CLI
   output.
2. Implement `delphi_lsp.agent_wiki` with safe atomic output, deterministic
   concept paths, cross-links, source fragments, problems, metrics, and
   protocol reference pages.
3. Wire `delphi-lsp-agent wiki export` into the CLI with root, optional project
   selection, output, worker, and force options.
4. Document the command and Open Knowledge Format semantics, bump public
   versions to 3.1.0, and add release notes.
5. Run focused and full verification, build and inspect both distributions,
   and run clean-install smoke tests.
6. Push the branch, merge only after green CI, then publish the exact main
   commit to PyPI and GitHub and verify the released artifacts.
