# tools

Tools for Tsonic.

## Clone all repositories

```sh
./clone-all.sh <container_dir>
```

For example, `./clone-all.sh /home/gorge/workspace/tsoniclang` creates one
checkout per repository directly inside that directory, such as
`/home/gorge/workspace/tsoniclang/tsonic` and
`/home/gorge/workspace/tsoniclang/tsonic-rust`.

Requires Bash, Git, curl, jq, sort, and working GitHub SSH access. Repository
discovery uses GitHub's paginated organization API, not a hardcoded list. It
includes forks and archived repositories, and does not use shallow clones.

Without `GITHUB_TOKEN`, discovery includes public repositories only. Export a
token with access to the required private repositories to include those too.
The token authenticates discovery; cloning uses your existing SSH credentials.
The script never prints the token or passes it in curl's command-line arguments.

Existing checkouts with the matching GitHub `origin` are left unchanged: no
pulling, branch switching, cleaning, or resetting. SSH and HTTPS origins are
recognized. Other existing paths, including symlinks, are refused without
modification. Clone failures do not prevent attempts at the remaining
repositories; the final summary reports failures and the exit status is nonzero.
Discovery failures stop before any cloning or destination creation.

- [GitHub organization repository API](https://docs.github.com/en/rest/repos/repos#list-organization-repositories)

## Clean build artifacts

```sh
./clean-all.sh <container_dir> --dry-run
./clean-all.sh <container_dir>
./clean-all.sh <container_dir> --dependencies
```

Requires Linux, Bash, Python 3.10+, and Git. **Stop builds, tests, dev servers,
and other workspace writers first.** The command without `--dry-run` deletes
artifacts immediately; it does not stop processes or coordinate concurrent writers.
It examines immediate child Git checkouts, not unrelated directories or global
package caches. Symlinked checkouts and mounted child directories are skipped.

Only Git-ignored files in recognized artifact locations, or with recognized
compiled-file names/content, are removed. Tracked files, staged files, unignored
new source, nested Git repositories/worktrees, secrets, symlinks, and **every
`.analysis` directory** are preserved. A `.analysis` nested inside `.temp` or
another output directory survives; the rest of that output can be cleaned.
Mixed source/output directories retain their protected files.

| Area | Examples cleaned |
| --- | --- |
| Scratch and tests | `.temp`, `.tmp`, `.test`, `.tests`, `temp`, `tmp`, repository `.cache` |
| C#/.NET | Project `bin`, `obj`, `Debug`, `Release`, `TestResults`, DLL/PDB/EXE/NuGet output |
| Rust | `target`, custom output containing `.rustc_info.json`, native object/library files |
| Python | `__pycache__`, bytecode, test/type/lint caches, wheels, egg metadata, coverage |
| C/C++ | `build`, CMake output/metadata, Ninja metadata, objects/libraries, ignored ELF executables |
| JavaScript/Node | `dist`, `out`, `build`, TypeScript build info, bundler caches, coverage |
| Mojo | `build`, `.mojo_cache`, `.mojoc`, `.mojopkg`, PTX/cubin and native binaries |

Local dependencies and environments (`node_modules`, `.pixi`, `.venv`, `venv`,
`env`, `.tox`, `.nox`, and related package stores) are preserved by default,
including inside scratch directories. Add `--dependencies` to clean their ignored
contents too. Protected names and Git-owned files still survive that option.

The report counts selected file-content bytes, **not guaranteed reclaimed disk
space**: sparse files, hardlinks and files still open in another process differ.
Unknown/custom artifact names remain untouched unless they occur beneath a
recognized output directory. Errors produce a nonzero exit status; cleanup is
not an atomic transaction. Do not treat this as a general `git clean -x`.
