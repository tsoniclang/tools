from __future__ import annotations

import argparse
from dataclasses import dataclass
import fnmatch
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


PROTECTED_NAMES = frozenset({
    ".analysis", ".git", ".hg", ".svn", ".codex", ".agents", ".secrets",
    ".ssh", ".gnupg", ".env", ".npmrc", ".pypirc", ".cargo", ".nuget",
})
DEPENDENCY_NAMES = frozenset({
    "node_modules", ".pixi", ".venv", "venv", "env", "ENV", "__pypackages__",
    ".tox", ".nox", ".eggs", ".npm", ".pnpm-store",
})
ARTIFACT_NAMES = frozenset({
    ".temp", ".tmp", ".test", ".tests", "temp", "tmp", ".cache", "target", "build",
    "dist", "out", "artifacts", "bld", ".tsonic", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".pyright", ".pyre", ".pytype", ".hypothesis",
    "htmlcov", "coverage", ".nyc_output", ".mojo_cache", ".next", ".nuxt",
    ".output", ".parcel-cache", ".turbo", ".vite", ".docusaurus", "CMakeFiles",
    "TestResults", "CodeCoverage", "pip-wheel-metadata",
})
ARTIFACT_PATTERNS = (
    "*.tsbuildinfo", "*.pyc", "*.pyo", "*.pyd", "*.o", "*.obj", "*.a",
    "*.so", "*.so.*", "*.dylib", "*.dll", "*.pdb", "*.exe", "*.node",
    "*.mojoc", "*.mojopkg", "*.ptx", "*.cubin", "*.bc", "*.whl",
    "*.nupkg", "*.snupkg", "*.binlog", "*.gcda", "*.gcno", "*.profraw",
    "*.profdata", ".coverage", ".coverage.*", "coverage.xml", ".ninja_deps",
    ".ninja_log", ".eslintcache", ".stylelintcache", "CMakeCache.txt", "cmake_install.cmake", "build.ninja",
)


@dataclass
class Counts:
    files: int = 0
    directories: int = 0
    bytes: int = 0
    retained: int = 0
    errors: int = 0

    def include(self, other: Counts) -> None:
        self.files += other.files
        self.directories += other.directories
        self.bytes += other.bytes
        self.retained += other.retained
        self.errors += other.errors


def protected_name(name: str) -> bool:
    return name in PROTECTED_NAMES or name.startswith(".env.")


def git_output(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        env={**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
             "GIT_OPTIONAL_LOCKS": "0"},
    ).stdout


def owned_paths(repository: Path) -> frozenset[str]:
    output = git_output(repository, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return frozenset(os.fsdecode(name).rstrip("/") for name in output.split(b"\0") if name)


def ignored_directory(repository: Path, relative: Path) -> bool:
    try:
        git_output(repository, "check-ignore", "--quiet", "--no-index", "--", relative.as_posix() + "/")
        return True
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        return False


def mount_paths() -> frozenset[Path]:
    records = Path("/proc/self/mountinfo").read_bytes().splitlines()
    return frozenset(Path(os.fsdecode(re.sub(
        rb"\\([0-7]{3})", lambda match: bytes([int(match[1], 8)]), record.split()[4],
    ))) for record in records)


def artifact_directory(name: str, contents: frozenset[str], siblings: frozenset[str]) -> bool:
    if name in ARTIFACT_NAMES or name.startswith("cmake-build-") or name.endswith(".egg-info"):
        return True
    if {"CMakeCache.txt", ".rustc_info.json"}.intersection(contents):
        return True
    return name.lower() in {"bin", "obj", "debug", "release"} and any(
        sibling.endswith((".csproj", ".fsproj", ".vbproj")) for sibling in siblings
    )


def artifact_file(name: str, descriptor: int, metadata: os.stat_result) -> bool:
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in ARTIFACT_PATTERNS):
        return True
    if not metadata.st_mode & 0o111:
        return False
    with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor), "rb") as stream:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError(f"File changed while inspecting executable: {name!r}")
        return stream.read(4) == b"\x7fELF"


def report(path: Path, counts: Counts, dry_run: bool) -> None:
    if not (counts.files or counts.directories or counts.retained or counts.errors):
        return
    action = "Would remove" if dry_run else "Removed"
    print(f"{action}: {str(path)!r}: {counts.files} files, {counts.directories} directories, "
          f"{counts.bytes / 1024**2:.2f} MiB file content; "
          f"{counts.retained} protected entries retained; {counts.errors} errors", flush=True)


def error_text(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError) and error.stderr:
        return error.stderr.decode(errors="replace").strip()
    return str(error)


def clean_repository(repository: Path, mounts: frozenset[Path], dry_run: bool, dependencies: bool) -> Counts:
    owned = owned_paths(repository)
    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_descriptor = os.open(repository, root_flags)
    device = os.fstat(root_descriptor).st_dev

    def visit(descriptor: int, relative: Path, active: bool, siblings: frozenset[str]) -> tuple[Counts, bool, bool]:
        path = repository / relative
        counts = Counts()
        with os.scandir(descriptor) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
        names = frozenset(entry.name for entry in entries)
        if relative != Path(".") and {".git", ".hg", ".svn"}.intersection(names):
            counts.retained = 1
            return counts, False, False
        candidate = artifact_directory(path.name, names, siblings) or (dependencies and path.name in DEPENDENCY_NAMES)
        selected = active or (relative != Path(".") and candidate and ignored_directory(repository, relative))
        removable = selected
        for entry in entries:
            child_relative = relative / entry.name
            child_path = repository / child_relative
            if protected_name(entry.name) or child_relative.as_posix() in owned:
                if selected:
                    counts.retained += 1
                removable = False
                continue
            if not dependencies and entry.name in DEPENDENCY_NAMES:
                if selected:
                    counts.retained += 1
                removable = False
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
                if metadata.st_dev != device or child_path in mounts:
                    counts.retained += 1
                    removable = False
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    child_descriptor = os.open(entry.name, root_flags, dir_fd=descriptor)
                    try:
                        current = os.fstat(child_descriptor)
                        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                            raise OSError(f"Directory changed while opening: {str(child_path)!r}")
                        child_counts, child_removable, child_selected = visit(child_descriptor, child_relative, selected, names)
                        counts.include(child_counts)
                        if child_removable:
                            current = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                                raise OSError(f"Directory changed before removal: {str(child_path)!r}")
                            if not dry_run:
                                os.rmdir(entry.name, dir_fd=descriptor)
                            child_counts.directories += 1
                            counts.directories += 1
                        else:
                            removable = False
                        if child_selected and not selected:
                            report(child_path, child_counts, dry_run)
                    finally:
                        os.close(child_descriptor)
                elif stat.S_ISREG(metadata.st_mode) and (selected or artifact_file(entry.name, descriptor, metadata)):
                    current = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                        metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size,
                    ):
                        raise OSError(f"File changed before removal: {str(child_path)!r}")
                    if not dry_run:
                        os.unlink(entry.name, dir_fd=descriptor)
                    removed = Counts(files=1, bytes=metadata.st_size)
                    counts.include(removed)
                    if not selected:
                        report(child_path, removed, dry_run)
                else:
                    if selected:
                        counts.retained += 1
                    removable = False
            except OSError as error:
                removable = False
                counts.errors += 1
                print(f"Cannot clean {str(child_path)!r}: {error}", file=sys.stderr)
        return counts, removable, selected

    try:
        return visit(root_descriptor, Path("."), False, frozenset())[0]
    finally:
        os.close(root_descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="clean-all.sh",
        description="Remove Git-ignored build artifacts from immediate child Git checkouts. Stop all builds first.",
    )
    parser.add_argument("container_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Report without deleting anything")
    parser.add_argument("--dependencies", action="store_true", help="Also remove ignored local dependencies and virtual environments")
    args = parser.parse_args()
    container = Path(os.path.abspath(args.container_dir))
    if sys.platform != "linux":
        parser.error("Linux is required for mount-boundary protection")
    if not container.is_dir() or container.resolve() != container:
        parser.error("Choose an existing real directory without symlink ancestors")
    if container == Path("/") or container == Path.home() or any(protected_name(part) for part in container.parts):
        parser.error("Refusing a root, home or protected directory")
    if (container / ".git").exists():
        parser.error("Select the container of repositories, not a repository itself")
    mounts = mount_paths()
    repositories = []
    for child in sorted(container.iterdir()):
        if child.is_symlink() or protected_name(child.name) or child in mounts:
            print(f"Protected container entry: {str(child)!r}", flush=True)
            continue
        if child.is_dir() and (child / ".git").exists():
            repositories.append(child)
    if not repositories:
        parser.error("No immediate child Git checkouts found")
    total = Counts()
    for repository in repositories:
        try:
            actual_root = os.fsdecode(git_output(repository, "rev-parse", "--show-toplevel")).rstrip("\n")
            if actual_root != str(repository):
                raise ValueError(f"Not a checkout root: {str(repository)!r}")
            result = clean_repository(repository, mounts, args.dry_run, args.dependencies)
            total.include(result)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            total.errors += 1
            print(f"Cannot clean repository {str(repository)!r}: {error_text(error)}", file=sys.stderr)
    print(f"\n{'Preview' if args.dry_run else 'Cleanup'}: {len(repositories)} repositories; "
          f"{total.files} files; {total.directories} directories; "
          f"{total.bytes / 1024**2:.2f} MiB file content; {total.errors} errors.")
    print(".analysis, Git state, tracked/unignored work, symlinks and mount boundaries are preserved.")
    return 1 if total.errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, subprocess.SubprocessError) as failure:
        print(f"Cleanup failed: {error_text(failure)}", file=sys.stderr)
        raise SystemExit(1)
