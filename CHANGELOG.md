# Changelog

Releases are tags named `release-X.Y.Z`; each tag carries its own notes, and the longer write-ups live in `docs/releases/`.

## 0.1.0

The first release. See [docs/releases/0.1.0.md](docs/releases/0.1.0.md).

The app is called Armada: commands start with `arm-` (`arm-run`, `arm-admin`, `arm-setup`, `arm-worker`, `arm-help`), the Python package is `armada`, environment variables start with `ARM_`, a folder's pin file is `.armada.toml`, and workers are called arms (`arm1`, `arm2`, ...). There is no migration from earlier names: uninstall the old version and install again.
