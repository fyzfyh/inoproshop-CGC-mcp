# Bundled third-party components

This distribution preserves the upstream license files. InoProShop, ohmypi,
model weights, accounts and engineering projects are not bundled.

| Component | Version | License/notice location in this package | Upstream |
|---|---|---|---|
| CPython (python-build-standalone Windows x64) | 3.12.7 / 20241016 | `runtime/python/LICENSE.txt` and the runtime's additional notices | https://github.com/astral-sh/python-build-standalone |
| Eclipse Temurin OpenJDK JRE | 21.0.6+7 | Original `offline/archives/java.zip`, including `jdk-21.0.6+7-jre/legal`; extracted on first run to `work/mcp_p0/neo4j/java` | https://github.com/adoptium/temurin21-binaries/releases/tag/jdk-21.0.6%2B7 |
| Neo4j Community | 5.26.12 | Original `offline/archives/neo4j.zip`, including `LICENSE.txt`, `NOTICE.txt` and `LICENSES.txt` where supplied; extracted to `work/mcp_p0/neo4j/runtime` | https://github.com/neo4j/neo4j/tree/5.26.12 |
| Python packages | Exact inventory in `offline/python-packages.json` | `runtime/python/Lib/site-packages/*.dist-info/` (including `licenses`, `LICENSE*`, `COPYING*`, `NOTICE*` as supplied) | Package metadata contains the corresponding upstream references |

The complete exported Python dependency lock, including upstream artifact
hashes, is `offline/python-requirements.lock`. Java and Neo4j original archive
URLs and SHA-256 hashes are in `services/inoproshop-mcp/launcher-runtime-lock.json`.
The per-file payload checksums are in `offline/manifest.json`.

Neo4j Community's own license governs its redistribution and use; the original
license and notices are preserved without modification. Corresponding upstream
source is available from the Neo4j repository/version above. OpenJDK source
release information is available at the Temurin release above. Consult the
bundled upstream terms for each component.

The source distribution contains the MCP service, bridge and launcher source.
No installed third-party runtime or database is copied from a developer's
live database directory. The builder creates a clean independent Python and
installs exact locked dependencies using local cached artifacts.

## Project license and corresponding source

The project's original code and documentation are licensed under MIT,
Copyright (c) 2026 fyzfyh. This grant does not replace the independent licenses,
copyrights or notices of bundled third-party components. See the root LICENSE.

The unmodified Neo4j Community and Temurin runtime corresponding sources are
available alongside the binary package on the public v2026.10.20.2 release,
at no additional charge and without a GitHub login:

- `neo4j-5.26.12-source.zip`: complete upstream tagged source, including the
  Maven build definitions and original licenses.
- `OpenJDK21U-jdk-sources_21.0.6_7.tar.gz`: the official Eclipse Temurin source
  release, including OpenJDK source and build configuration.
- The official Temurin source SHA-256 and source/runtime build metadata JSON
  files, and `THIRD-PARTY-SOURCES.json`, record the exact upstream artifacts,
  source revision, build provenance and checksums. The source tarball is
  verified against its official upstream SHA-256.
- `certifi-2026.7.22.tar.gz`, `pathspec-1.1.1.tar.gz`, `pyzmq-27.2.0.tar.gz`
  and `libzmq-4.3.5-source.tar.gz` provide the corresponding MPL component
  sources and pyzmq's bundled libzmq build definitions. Python source archives
  are verified against the unchanged project dependency lock.

Public release: https://github.com/fyzfyh/inoproshop-CGC-mcp/releases/tag/v2026.10.20.2

The maintained provenance inventory is `docs/maintenance/THIRD_PARTY_SOURCES.json`
relative to the package root. Python package source modules and supplied
license files remain in the portable runtime; each component retains its own
terms. OpenJDK's GPL with Classpath exception and Neo4j Community's GPL are
not relicensed as MIT. No bundled upstream binary is modified by this release.

The same source materials apply to the identical runtime binaries in the
2026-10-20 and v2026.10.20.1 packages. Their original archives, licenses and
checksums are preserved.
