# Client packages

`hivemind-memory` lets you build your own agent or system on Hivemind, outside the harnesses the [agent plugin](../plugins/hivemind/README.md) supports. It connects to a Hivemind MCP endpoint and gives your agent loop the tools, the instructions the plugin gives its harnesses, and the end-of-task reminder. Why it is built this way is in [ADR 0162](../docs/adr/0162-python-and-npm-client-packages.md). Building on the Claude Agent SDK? Load the plugin itself instead; the package READMEs show how.

| Folder | Registry | Docs |
|---|---|---|
| `python/` | PyPI, `pip install hivemind-memory` | [python/README.md](python/README.md) |
| `typescript/` | npm, `npm install hivemind-memory` | [typescript/README.md](typescript/README.md) |

To start, you need what an agent plugin user needs: the MCP address of your organization's Hivemind (it ends in `/mcp`) and an agent key from its admin (or the org key, to let your agent register itself first). The package READMEs take it from there, with a whole agent loop to copy.

## For maintainers

Both packages carry the server's version, and ship copies of the `hivemind` skill and the Stop hook's reminder. The skill copy leaves out what only the plugin has (the setup skill, incognito sessions), as `sync_text.py` lists ([ADR 0163](../docs/adr/0163-client-skill-copy-drops-plugin-only-lines.md)). After changing either, run `make sync-client-text`; a unit test fails until the copies match, and the script fails if a passage it rewords has changed.

`live_server.py` starts the real HTTP MCP runner with a fresh contributor agent for the packages' live tests. CI's **Client packages** job runs them.

### Publishing

Pushing a release tag `vX.Y.Z` runs `.github/workflows/publish-clients.yml`, which publishes both packages with trusted publishing (GitHub OIDC, no stored token). One job builds both packages without access to the publishing token; the PyPI and npm jobs only upload what it built. It does nothing until the repository variable `PUBLISH_CLIENTS` is `true`, and it skips a version a registry already has, so a re-run is safe.

#### One-time setup

This repository did it with 2.31.0; a fork that publishes under its own names needs it once. npm can only trust a workflow for a package that already exists, so the first npm release is published by hand.

1. **GitHub environments.** In the repository's Settings, Environments, create `pypi` and `npm`. Add yourself as a required reviewer on both if you want to approve each publish.
2. **PyPI pending publisher.** On pypi.org, Your account, Publishing, add a pending publisher: project `hivemind-memory`, owner `monochrome-muncher`, repository `hivemind`, workflow `publish-clients.yml`, environment `pypi`.
3. **Tag the release** as usual. The publish run is skipped, because `PUBLISH_CLIENTS` is not set yet.
4. **First npm publish, by hand**, from the tag, signed in to npm with two-factor authentication on:

   ```sh
   git checkout vX.Y.Z
   cd clients/typescript
   npm ci && npm run build
   npm login
   npm publish --access public
   ```

5. **npm trusted publisher.** On npmjs.com, the `hivemind-memory` package, Settings, Trusted publishing, add GitHub Actions: user `monochrome-muncher`, repository `hivemind`, workflow `publish-clients.yml`, environment `npm`. You can then set publishing access to disallow tokens.
6. **Turn it on.** In Settings, Secrets and variables, Actions, Variables, add `PUBLISH_CLIENTS` with the value `true`.
7. **Publish the PyPI side of that release.** In Actions, run **Publish client packages** with "Use workflow from" set to the tag `vX.Y.Z`. PyPI publishes; npm sees the version is there and skips.

From the next release on, the tag push publishes both.
