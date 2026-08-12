"""A PR diff turned into a quizzable corpus (no I/O) - unit-tested in tests/test_diff_corpus.py.

Every judgement about what a reviewer is asked to look at: which paths are
machine-generated, which changes GitHub will not show, how a deletion travels
with the code that explains it, and how the surviving patches are cut into
per-prompt chunks. github_diff.py does the fetching; this module decides what
the fetch was worth.
"""
import difflib
import fnmatch
import posixpath
from itertools import islice
from typing import NamedTuple

CHUNK_CHAR_BUDGET = 30_000
MAX_CHUNKS = 5  # 5 x 30k chars keeps total diff coverage at the old single-prompt limit
DELETED_PREVIEW_LINES = 30  # enough to tell a one-line helper from a state machine
DELETED_BLOCK_SHARE = 3  # deletions may claim at most 1/3 of a chunk's budget
DELETED_BLOCK_HEADER = "Files deleted by this pull request:"
DELETED_NAMES_LISTED = 30
MIN_REFERENCE_STEM = 4  # "main"/"util" are everywhere; a wrong hint beats no hint
# A rebuilt patch past this cannot fit a chunk anyway (CHUNK_CHAR_BUDGET would
# truncate it mid-hunk); stopping at a line boundary at least ends on a real one.
RECONSTRUCTED_PATCH_LINES = 2_000
# GitHub collapses these in its own diff view, so quizzing them tests nothing.
# Lowercase by construction: is_generated_path lowers the path before matching.
# Deliberately absent: *.ipynb (hand-written source), dist/ and build/ (too
# project-specific to guess - that is what the configured globs are for).
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
    # no "*/vendor/*" twin needed: globs match at any depth (_path_variants)
    "vendor/*",
    "node_modules/*",
)


def _expand_glob(pattern):
    """Normalize one configured path pattern onto what is_generated_path matches.

    Shared by both configured sources (the adopter's globs and the repo's
    .gitattributes) so a pattern means the same thing wherever it is written.
    Anchoring is not reproduced; "**" expands to both the zero-directory and the
    one-or-more-directory form. A match-everything pattern is dropped rather than
    obeyed - fnmatch's "*" crosses "/", so honoring one would class every file in
    every PR as generated and waive the gate repo-wide.

    A trailing slash means the directory's contents, the way git reads it. A
    pattern WITHOUT one keeps git's other rule and matches a file of that name at
    any depth, so "dist" is not "dist/" - _path_variants already supplies the
    depth, and inventing the directory sense here would diverge from what the
    same line means to git.

    Returns 0, 1 or 2 globs.
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
        # Without this the glob only ever matches a FILE called "dist", i.e.
        # nothing: every path under dist/ has more path left after the name.
        glob += "/*"
    expanded = (
        (glob.replace("/**/", "/"), glob.replace("/**/", "/*/"))
        if "/**/" in glob
        else (glob,)
    )
    # After normalization, never before: "**/*" and "*/*" survive the checks above
    # and collapse into a match-everything glob only at this point. fnmatch's "*"
    # crosses "/", so a glob built only from "*" and "/" matches every path
    # variant, and honoring one would waive the gate repo-wide - too much damage
    # for one stray character in a config string.
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

    fnmatchcase over a pre-lowered path, never fnmatch: fnmatch normcases via the
    HOST os, so a glob would behave differently on Windows and on the Linux job.
    """
    globs = GENERATED_PATH_GLOBS + tuple(g.lower() for g in extra_globs)
    variants = _path_variants(path.lower())
    return any(
        fnmatch.fnmatchcase(variant, glob) for variant in variants for glob in globs
    )


def parse_gitattributes_generated(text):
    """Globs a .gitattributes marks as linguist-generated, in file order.

    A repo that already declares its generated paths gets them skipped with no
    extra configuration. Only the forms that SET the attribute count; the unset
    forms ("-linguist-generated", "=false") are skipped. Patterns go through
    _expand_glob, the same translation the adopter's globs get.
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

    A hint, not a proof: a file that stopped importing the deleted module is the
    whole answer to "what does removing this break", and a substring search finds
    it without spending a model call. Matches the full path, the basename and the
    extension-free stem. Blind to re-exports, dynamic imports and files the PR
    never touched, hence "hints" in the rendered header.
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

    The excerpt is the head of the removal patch, where a file explains itself -
    a filename alone cannot tell a one-line helper from a state machine. Capped so
    a 4000-line deletion cannot crowd out the surviving code the answer needs.
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

    Files whose excerpt does not fit the budget are still named rather than
    dropped silently, and that naming line is budgeted too: chunk_files hands out
    the rest of the chunk on the promise that this block fits `budget`. The
    reserve is sized from the worst case, every file named, so the line that
    actually renders - always a subset - can only be shorter.
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

    GitHub omits `patch` for a text diff it considers too large. Rebuilding it
    keeps that change inside the corpus AND inside the question count; dropping
    it took its changed lines out of the sizing too, which is what made
    "pad a file until GitHub stops diffing it" a way past the gate.

    Headerless, like GitHub's own patch text: the ---/+++ file lines are dropped
    by position, never by prefix, because a removed source line of "---" renders
    as "----" and would take a prefix test with it.
    """
    # islice(.., 2, None) drops the two file headers by position; both are absent
    # entirely when there is no diff, and islice is fine with that.
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
    """True when a patchless record is real content GitHub simply would not show.

    GitHub omits `patch` for two different reasons and reports them differently:
    a binary file comes back with zero changed lines, while a text diff it
    considers too large keeps its real line counts. Only the second is source
    someone was meant to review, and treating the two alike is what makes
    "pad the diff until GitHub drops it" a way past the gate.
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
    Generated content is dropped outright; a deleted file is kept as context but
    weighs nothing, since the one thing worth asking about it is the consequence
    of the removal; and a patchless file cannot be put in front of anyone, so it
    is neither shown nor counted. Both kinds of drop are reported rather than
    silent.

    edits_gitattributes rides along because it is the one thing that makes an
    empty corpus unsafe to waive: such a PR is rewriting the very rules the
    emptiness was decided by. An undiffable change is deliberately not a second
    reason - it lands in `unreviewable`, which fails the run outright, before a
    waive is ever considered.
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
    max_chunks chunks exist, files that do not fit the last chunk are dropped
    (bounds model calls, like the old single-prompt character truncation).

    Each deleted file goes into ONE chunk: the one holding a surviving file that
    referenced it, else the closest by path. Its impact is only visible against
    the code that remains, so it has to travel with that code rather than sit in
    whichever chunk provider order happened to put it in - and repeating it in
    every chunk would just buy the same question several times over. Deleted
    files carry zero changed_lines, so they never shift question allocation.
    """
    deleted = [f for f in files if is_deleted(f)]
    # Sized to the blocks that actually exist, capped at DELETED_BLOCK_SHARE.
    # A flat share charged every chunk for room a two-line deletion never uses,
    # which cost a big PR a third of the diff the model ever sees.
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

    A chunk that changed a file referencing the deletion wins outright - that
    pairing is the whole answer to what the removal broke. Failing that, the
    chunk sharing the longest directory prefix, so a deletion at least lands
    near its own part of the tree.
    """
    for i, names in enumerate(chunk_filenames):
        if any(name in names for name in deleted.get("references", ())):
            return i
    parts = deleted["filename"].split("/")[:-1]
    # commonprefix compares element-wise on lists, so this counts shared
    # directories, not shared characters.
    scores = [
        max((len(posixpath.commonprefix([parts, n.split("/")[:-1]])) for n in names),
            default=0)
        for names in chunk_filenames
    ]
    return scores.index(max(scores)) if scores else 0
