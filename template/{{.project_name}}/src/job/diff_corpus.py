"""A PR diff turned into a quizzable corpus (no I/O).

Decides what a reviewer is asked to look at: which paths are generated, which
changes GitHub will not show, and where the chunk boundaries fall. github_diff.py
does the fetching. Unit-tested in tests/test_diff_corpus.py.
"""
import difflib
import fnmatch
import posixpath
from itertools import islice
from typing import NamedTuple

CHUNK_CHAR_BUDGET = 30_000
MAX_CHUNKS = 5  # 5 x 30k keeps total coverage at the old single-prompt limit
DELETED_PREVIEW_LINES = 30  # enough to tell a one-line helper from a state machine
DELETED_BLOCK_SHARE = 3  # deletions may claim at most 1/3 of a chunk's budget
DELETED_BLOCK_HEADER = "Files deleted by this pull request:"
DELETED_NAMES_LISTED = 30
MIN_REFERENCE_STEM = 4  # "main"/"util" are everywhere; too short to be a hint
# A longer rebuilt patch cannot fit a chunk anyway; cutting here ends on a real line.
RECONSTRUCTED_PATCH_LINES = 2_000
# GitHub collapses these in its own diff view, so quizzing them tests nothing.
# Lowercase: is_generated_path lowers the path before matching. dist/ and build/
# are absent on purpose - too project-specific, that is what the config globs are for.
GENERATED_PATH_GLOBS = (
    # "*.lock" covers uv, poetry, pipenv, pdm, cargo, yarn, bundler, composer,
    # flake and mix; the rest spell theirs differently.
    "*.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "packages.lock.json",
    "gradle.lockfile",
    "bun.lockb",
    "package.resolved",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.snap",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
    "*_generated.go",
    # no "*/vendor/*" twin: globs match at any depth (_path_variants)
    "vendor/*",
    "node_modules/*",
)


def _expand_glob(pattern):
    """Normalize one configured path pattern onto what is_generated_path matches.

    Used for both the adopter's globs and the repo's .gitattributes, so a pattern
    means the same thing in either place. Anchoring is not reproduced. "**" expands
    to two globs: zero directories, and one or more. Returns 0, 1 or 2 globs.
    """
    stripped = pattern.strip()
    glob = stripped.strip("/")
    if not glob or glob.startswith("!"):
        return ()
    while glob.startswith("**/"):
        glob = glob[3:]
    if glob.endswith("/**"):
        glob = glob[:-3] + "/*"
    if stripped.endswith("/") and not glob.endswith("*"):
        # A trailing slash means the directory's contents, the way git reads it.
        # Otherwise the glob only matches a FILE called "dist", which is nothing.
        glob += "/*"
    expanded = (
        (glob.replace("/**/", "/"), glob.replace("/**/", "/*/"))
        if "/**/" in glob
        else (glob,)
    )
    # Drop a match-everything glob, and do it after normalization: "**/*" and "*/*"
    # only collapse into one here. fnmatch's "*" crosses "/", so obeying one would
    # class every file as generated and waive the gate repo-wide.
    return tuple(g for g in expanded if g and not set(g) <= {"*", "/"})


def parse_glob_list(raw):
    """Split an adopter-supplied comma-separated glob list into clean globs."""
    return tuple(g for part in (raw or "").split(",") for g in _expand_glob(part))


def _path_variants(path):
    """`path` plus every sub-path starting at a directory boundary.

    "web/dist/app.js" -> ("web/dist/app.js", "dist/app.js", "app.js"), so a glob
    applies at any depth the way git reads an unanchored pattern.
    """
    parts = path.split("/")
    return tuple("/".join(parts[i:]) for i in range(len(parts)))


def is_generated_path(path, extra_globs=()):
    """True when `path` looks machine-generated rather than authored.

    Always fnmatchcase over a pre-lowered path, never fnmatch: fnmatch case-folds
    per the HOST os, so one glob would behave differently on Windows and on Linux.
    """
    globs = GENERATED_PATH_GLOBS + tuple(g.lower() for g in extra_globs)
    variants = _path_variants(path.lower())
    return any(
        fnmatch.fnmatchcase(variant, glob) for variant in variants for glob in globs
    )


def parse_gitattributes_generated(text):
    """Globs a .gitattributes marks as linguist-generated, in file order.

    Only the forms that SET the attribute count. The unset forms
    ("-linguist-generated", "=false") are skipped.
    """
    globs = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # "[attr]foo ..." defines a macro, not a path rule
        if not line or line.startswith("#") or line.startswith("["):
            continue
        pattern, *attrs = line.split()
        if not any(a in ("linguist-generated", "linguist-generated=true") for a in attrs):
            continue
        globs.extend(_expand_glob(pattern))
    return tuple(globs)


def is_deleted(f):
    """True when a raw or corpus file record describes a file the PR removes."""
    return f.get("status") == "removed"  # GitHub's status word for a deletion


def deletion_references(deleted_path, others):
    """Paths among `others` [(filename, patch)] whose patch text mentions `deleted_path`.

    A hint, not a proof. A substring search finds the caller that stopped importing
    the deleted module, without a model call. It is blind to re-exports, dynamic
    imports, and files the PR never touched.
    """
    base = posixpath.basename(deleted_path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    needles = {deleted_path, base}
    if len(stem) >= MIN_REFERENCE_STEM:
        needles.add(stem)
    return tuple(
        name for name, patch in others if any(n in (patch or "") for n in needles)
    )


def deleted_file_text(filename, changed_lines, patch, references,
                      preview_lines=DELETED_PREVIEW_LINES):
    """One deleted file's block: what it was, who referenced it, a capped excerpt.

    The excerpt is the head of the removal patch, where a file explains itself.
    Capped so a 4000-line deletion cannot crowd out the surviving code.
    """
    lines = [f"--- {filename} (DELETED by this PR)"]
    if references:
        lines.append(f"    referenced in changed files: {', '.join(references)}")
    removed = (patch or "").splitlines()
    if not removed:
        # GitHub returns no patch for a binary or oversized removal
        lines.append(f"    {changed_lines} lines removed; contents not available")
        return "\n".join(lines)
    shown, rest = removed[:preview_lines], removed[preview_lines:]
    scope = f"first {len(shown)} of {len(removed)}" if rest else f"all {len(removed)}"
    lines.append(f"    {scope} removed lines, for context only:")
    lines.extend(shown)
    if rest:
        lines.append("    [excerpt truncated]")
    return "\n".join(lines)


def deletions_size(deleted):
    """Characters deletion_block needs to render every excerpt in full."""
    return len(DELETED_BLOCK_HEADER) + sum(len(f["text"]) + 2 for f in deleted)


def _overflow_note(names):
    """The line naming deletions whose excerpt did not fit."""
    shown = names[:DELETED_NAMES_LISTED]
    note = ", ".join(shown)
    if len(names) > len(shown):
        note += f", and {len(names) - len(shown)} more"
    return ("    also deleted, named without context to leave room for the "
            f"surviving code: {note}")


def deletion_block(deleted, budget):
    """The deletion context chunk_files prepends to the chunk they belong with.

    A file whose excerpt does not fit is still named, and that naming line is
    budgeted too: chunk_files spends the rest of the chunk on the promise that this
    block fits `budget`. The reserve assumes every file is named, so the rendered
    line can only be shorter.
    """
    if not deleted:
        return ""
    reserve = (
        0 if deletions_size(deleted) <= budget
        else len(_overflow_note([f["filename"] for f in deleted])) + 2
    )
    parts = [DELETED_BLOCK_HEADER]
    used = len(DELETED_BLOCK_HEADER)
    listed_only = []
    for f in deleted:
        if used + len(f["text"]) + 2 <= budget - reserve:
            parts.append(f["text"])
            used += len(f["text"]) + 2
        else:
            listed_only.append(f["filename"])
    if listed_only:
        parts.append(_overflow_note(listed_only))
    return "\n\n".join(parts)


def render_patch(old_text, new_text):
    """A capped unified diff of two file versions, shaped like GitHub's `patch`.

    GitHub omits `patch` for a text diff it considers too large. Rebuilding keeps
    that change in the corpus and in the question count. Without it, padding a file
    until GitHub stops diffing it is a way past the gate.
    """
    # islice(.., 2, None) drops the ---/+++ header lines by POSITION. A prefix test
    # would also eat a removed source line of "---", which renders as "----".
    # Both headers are absent when there is no diff; islice is fine with that.
    diff = islice(
        difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(), lineterm="", n=3
        ),
        2,
        None,
    )
    lines = list(islice(diff, RECONSTRUCTED_PATCH_LINES))
    if next(diff, None) is not None:
        lines.append(f"[rebuilt diff truncated at {RECONSTRUCTED_PATCH_LINES} lines]")
    return "\n".join(lines)


def is_unreviewable(f):
    """True when a patchless record is real content GitHub would not show.

    GitHub omits `patch` for two reasons and reports them differently. A binary file
    comes back with zero changed lines. A too-large text diff keeps its real counts,
    and only that one is source someone was meant to review.
    """
    return not f.get("patch") and not is_deleted(f) and f.get("changed_lines", 0) > 0


class PreparedDiff(NamedTuple):
    """What the job needs about a PR's diff, once the unreviewable parts are out."""

    files: list  # quizzable corpus, in provider order
    changed_lines: int  # sizing weight; NOT the PR's raw changed-line count
    skipped_generated: tuple  # dropped as machine-generated, for the log
    unreviewable: tuple  # real changes GitHub would not show, for the log
    edits_gitattributes: bool  # the one reason an empty corpus may not be waived


def prepare_files(raw, generated_globs=()):
    """Normalize provider file records into the quiz corpus plus its sizing weight.

    `raw` holds one {'filename', 'status', 'changed_lines', 'patch'} dict per file
    the PR touches, in provider order.

    Only changes a reviewer is asked to understand reach the corpus or the weight.
    Generated content is dropped. A deleted file is kept as context but weighs
    nothing, because the only question worth asking is what the removal breaks. A
    patchless file is neither shown nor counted. Both drops are reported.
    """
    kept, skipped = [], []
    for f in raw:
        target = skipped if is_generated_path(f["filename"], generated_globs) else kept
        target.append(f)
    # One deleted file citing another says nothing about what the removal breaks.
    surviving = [
        (f["filename"], f["patch"]) for f in kept if f.get("patch") and not is_deleted(f)
    ]
    files = []
    total = 0
    for f in kept:
        if is_deleted(f):
            references = deletion_references(f["filename"], surviving)
            files.append(
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "text": deleted_file_text(
                        f["filename"], f["changed_lines"], f.get("patch"), references
                    ),
                    "changed_lines": 0,
                    "references": references,
                }
            )
            continue
        if f.get("patch"):
            total += f["changed_lines"]
            files.append(
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "text": f"--- {f['filename']} ({f['status']})\n{f['patch']}",
                    "changed_lines": f["changed_lines"],
                    "references": (),
                }
            )
    return PreparedDiff(
        files,
        total,
        tuple(f["filename"] for f in skipped),
        tuple(f["filename"] for f in kept if is_unreviewable(f)),
        any(posixpath.basename(f["filename"]) == ".gitattributes" for f in raw),
    )


def chunk_files(files, budget=CHUNK_CHAR_BUDGET, max_chunks=MAX_CHUNKS):
    """Group whole per-file patches, in input order, into per-prompt diff chunks.

    A single file longer than budget becomes its own chunk, truncated. Once
    max_chunks chunks exist, files that do not fit the last chunk are dropped, which
    bounds the number of model calls.

    Each deleted file goes into ONE chunk, picked by assign_deletion. Repeating it
    in every chunk would buy the same question several times. Deleted files carry
    zero changed_lines, so they never shift question allocation.
    """
    deleted = [f for f in files if is_deleted(f)]
    # Sized to the blocks that exist, capped at DELETED_BLOCK_SHARE. A flat share
    # charged every chunk for room a two-line deletion never uses.
    reserve = (
        max(1, min(budget // DELETED_BLOCK_SHARE, deletions_size(deleted)))
        if deleted else 0
    )
    # floors at 1 so an absurdly small budget cannot make the slice below nonsense
    room = max(budget - reserve - 2, 1) if reserve else budget
    chunks = []
    for f in files:
        if is_deleted(f):
            continue
        if chunks:
            joined = chunks[-1]["text"] + "\n\n" + f["text"]
            if len(joined) <= room:
                chunks[-1]["text"] = joined
                chunks[-1]["changed_lines"] += f["changed_lines"]
                chunks[-1]["filenames"].append(f["filename"])
                continue
        if len(chunks) < max_chunks:
            chunks.append(
                {
                    "text": f["text"][:room],
                    "changed_lines": f["changed_lines"],
                    "filenames": [f["filename"]],
                }
            )
    if not deleted:
        return chunks
    if not chunks:
        chunks.append({"text": "", "changed_lines": 0, "filenames": []})
    assigned = [[] for _ in chunks]
    for f in deleted:
        assigned[assign_deletion(f, [c["filenames"] for c in chunks])].append(f)
    for chunk, group in zip(chunks, assigned):
        block = deletion_block(group, reserve)
        if not block:
            continue
        chunk["text"] = f"{block}\n\n{chunk['text']}" if chunk["text"] else block
        chunk["filenames"] = [f["filename"] for f in group] + chunk["filenames"]
    return chunks


def assign_deletion(deleted, chunk_filenames):
    """Index of the chunk a deleted file belongs with.

    A chunk that changed a file referencing the deletion wins outright: that pairing
    is the answer to what the removal broke. Otherwise, the chunk that shares the
    longest directory prefix.
    """
    for i, names in enumerate(chunk_filenames):
        if any(name in names for name in deleted.get("references", ())):
            return i
    parts = deleted["filename"].split("/")[:-1]
    # commonprefix on lists compares element-wise: shared directories, not characters.
    scores = [
        max((len(posixpath.commonprefix([parts, n.split("/")[:-1]])) for n in names),
            default=0)
        for names in chunk_filenames
    ]
    return scores.index(max(scores)) if scores else 0
