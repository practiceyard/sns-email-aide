# SPDX-License-Identifier: MIT-0
"""Keep the inline Lambda code in the CloudFormation template in sync with the
standalone source files.

The template embeds each Lambda's code inline (Resources.<Fn>.Properties.Code.
ZipFile) so it deploys as raw YAML with no build step. The standalone copies
under src/lambda/<name>/index.py are what we edit and unit-test. Without a
mechanism these two drift apart on nothing but good intentions; this script is
that mechanism.

Modes:
  --check  (default) exit non-zero if any inline block differs from its source.
  --sync             rewrite the template's inline blocks from the sources.

The inline block is required to be byte-identical to its source file, so the
comparison is a simple string compare and --sync is a simple splice. Only the
ZipFile blocks are touched; the rest of the template is left byte-for-byte.
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TEMPLATE = os.path.join(REPO, "sns-email-aide.yaml")

# logical resource id (as it appears "  <Id>:" in the template) -> source file
LAMBDAS = {
    "InterfaceLambda": os.path.join(REPO, "src", "lambda", "interface", "index.py"),
    "ProcessorLambda": os.path.join(REPO, "src", "lambda", "processor", "index.py"),
}


class SyncError(Exception):
    pass


def _read(path):
    """Read a file with line endings normalized to '\\n' for comparison."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n").replace("\r", "\n")


def _detect_newline(path):
    with open(path, "rb") as f:
        return "\r\n" if b"\r\n" in f.read() else "\n"


# A block-scalar opener for a Lambda's inline code: "Code:" then "ZipFile: |",
# each indented one level (2 spaces) deeper than the last. The content of the
# block is always indented deeper still, so an occurrence of the literal
# "ZipFile: |" inside the embedded Python can never match this pattern.
_ZIPFILE_OPENER = re.compile(r"^(?P<indent> +)ZipFile: \|\s*$")
_CODE_KEY = re.compile(r"^ +Code:\s*$")


def _zipfile_openers(lines):
    """Indices of every structural `ZipFile: |` block-scalar opener that sits
    directly under a `Code:` key. Used to assert the template holds exactly the
    Lambda blocks we expect and nothing has shadowed or duplicated one."""
    openers = []
    for n, ln in enumerate(lines):
        m = _ZIPFILE_OPENER.match(ln.rstrip("\r\n"))
        if not m:
            continue
        zip_indent = len(m.group("indent"))
        # The preceding non-blank line must be `Code:` one level shallower.
        prev = next((lines[k].rstrip("\r\n") for k in range(n - 1, -1, -1)
                     if lines[k].strip() != ""), "")
        prev_indent = len(prev) - len(prev.lstrip())
        if _CODE_KEY.match(prev) and prev_indent == zip_indent - 2:
            openers.append(n)
    return openers


def assert_zipfile_integrity(lines):
    """Fail if the template does not contain exactly one inline Lambda block per
    entry in LAMBDAS. This is the structural backstop for the text splice: a
    decoy `ZipFile:` under the wrong key, a duplicated block, or a missing one
    is caught here rather than silently mis-synced."""
    openers = _zipfile_openers(lines)
    if len(openers) != len(LAMBDAS):
        raise SyncError(
            f"expected {len(LAMBDAS)} inline ZipFile block(s) under a Code: key, "
            f"found {len(openers)}; template structure is not as expected")


def _find_block(lines, logical_id):
    """Locate a Lambda's inline ZipFile block.

    Returns (start_idx, end_idx, indent) where start_idx..end_idx (exclusive)
    are the content lines of the block and indent is their common leading
    whitespace. Raises SyncError if the resource or its ZipFile is not found.
    """
    # Find "  <logical_id>:" (2-space indent, top-level resource).
    res_marker = f"  {logical_id}:"
    i = next((n for n, ln in enumerate(lines) if ln.rstrip("\r\n") == res_marker), None)
    if i is None:
        raise SyncError(f"resource {logical_id} not found in template")

    # Find the block-scalar opener after it (before the next top-level
    # resource). Only a real "ZipFile: |" under a "Code:" key qualifies, so a
    # decoy "ZipFile:" elsewhere in the resource cannot shadow it.
    resource_openers = set(_zipfile_openers(lines))
    zip_idx = None
    for n in range(i + 1, len(lines)):
        stripped = lines[n].rstrip("\r\n")
        if stripped.startswith("  ") and not stripped.startswith("   ") and stripped.endswith(":"):
            break  # next top-level resource; stop
        if n in resource_openers:
            zip_idx = n
            break
    if zip_idx is None:
        raise SyncError(f"ZipFile block not found for {logical_id}")

    zip_indent = len(lines[zip_idx]) - len(lines[zip_idx].lstrip())
    content_indent = None
    start = zip_idx + 1
    end = start
    for n in range(start, len(lines)):
        line = lines[n]
        if line.strip() == "":
            end = n + 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= zip_indent:
            break  # block ended (dedent to ZipFile's level or shallower)
        if content_indent is None:
            content_indent = indent
        end = n + 1
    if content_indent is None:
        raise SyncError(f"empty ZipFile block for {logical_id}")
    return start, end, content_indent


def _inline_text(lines, start, end, indent):
    """Extract the block content, dedented, as it should match the source file."""
    out = []
    for line in lines[start:end]:
        if line.strip() == "":
            out.append("")
        else:
            out.append(line[indent:].rstrip("\n"))
    # Drop trailing blank lines that are block padding, then re-add one newline
    # so it compares against a source file that ends in a single newline.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def _reindent(source, indent):
    pad = " " * indent
    lines = source.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # source's trailing newline
    return [(pad + ln + "\n") if ln.strip() != "" else "\n" for ln in lines]


def check():
    template = _read(TEMPLATE)
    lines = template.splitlines(keepends=True)
    assert_zipfile_integrity(lines)
    problems = []
    for logical_id, src_path in LAMBDAS.items():
        start, end, indent = _find_block(lines, logical_id)
        inline = _inline_text(lines, start, end, indent)
        source = _read(src_path)
        if inline != source:
            problems.append((logical_id, src_path))
    if problems:
        print("Inline Lambda code is OUT OF SYNC with source:")
        for logical_id, src_path in problems:
            rel = os.path.relpath(src_path, REPO)
            print(f"  - {logical_id} != {rel}")
        print("\nRun: python scripts/sync_lambda_code.py --sync")
        return 1
    print("Inline Lambda code is in sync with source.")
    return 0


def sync():
    template = _read(TEMPLATE)
    lines = template.splitlines(keepends=True)
    assert_zipfile_integrity(lines)
    # Rewrite from the bottom up so earlier indices stay valid.
    changed = []
    for logical_id, src_path in sorted(
        LAMBDAS.items(), key=lambda kv: _find_block(lines, kv[0])[0], reverse=True
    ):
        start, end, indent = _find_block(lines, logical_id)
        inline = _inline_text(lines, start, end, indent)
        source = _read(src_path)
        if inline != source:
            lines[start:end] = _reindent(source, indent)
            changed.append(logical_id)
    if changed:
        newline = _detect_newline(TEMPLATE)
        text = "".join(lines)
        if newline == "\r\n":
            text = text.replace("\n", "\r\n")
        with open(TEMPLATE, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print("Synced inline code from source for: " + ", ".join(sorted(changed)))
    else:
        print("Already in sync; nothing to write.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true",
                        help="rewrite inline template code from source (default: check only)")
    args = parser.parse_args(argv)
    try:
        return sync() if args.sync else check()
    except SyncError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
