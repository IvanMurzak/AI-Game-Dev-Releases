# AI Game Dev — desktop builds

Download AI Game Dev from **https://ai-game.dev/download**.

This repository holds only mirrored copies of the published desktop installers. The official
source is `downloads.ai-game.dev`; every file here is copied byte for byte and checked against the
sha512 in the official update feed before upload. The desktop app's auto-updater verifies the same
sha512 after download, whichever host served the file.

Each release also carries the update feeds `latest.yml`, `latest-mac.yml` and `latest-linux.yml`
(the official feeds with file names pointing at this release's assets), added only once every file
of that release is mirrored. The newest such release (normally the one the official feeds name) is
marked latest, so
`https://github.com/IvanMurzak/AI-Game-Dev-Releases/releases/latest/download` serves as a fallback
update feed when `downloads.ai-game.dev` cannot be reached.

No source code lives here.
