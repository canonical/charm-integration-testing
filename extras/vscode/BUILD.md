## Build zenbones themes

`make`

## Update version

Edit `package.json`.

## Generate package.json

`./generate.sh`

## Debug extension

Press F5 in VS Code.

## Package extension

`vsce package --no-dependencies`

## Publish extension

`vsce publish --no-dependencies`

## Publish from GitHub

In the
[Marketplace publisher management page](https://marketplace.visualstudio.com/manage),
create a Trusted Publishing policy for the `rpbritton` publisher with repository
owner `rpbritton`, repository `zenbones.vscode`, and workflow
`publish-vscode.yml`. Leave the environment field empty. No repository secret is
needed. See the
[Trusted Publishing documentation](https://code.visualstudio.com/api/working-with-extensions/publishing-extension#publish-with-github-actions-and-trusted-publishing).

Bump `version` in `package.json` to a version newer than the published version,
then push a tag named `vscode-v<version>` (for example, `vscode-v0.0.3`). The
GitHub workflow rebuilds all themes and publishes the extension after confirming
that the tag matches the package version.
