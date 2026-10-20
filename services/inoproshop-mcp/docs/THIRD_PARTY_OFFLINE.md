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
