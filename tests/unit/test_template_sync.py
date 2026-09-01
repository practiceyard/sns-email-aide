# SPDX-License-Identifier: MIT-0
"""Guardrail: the inline Lambda code in the CloudFormation template must stay
byte-identical to the standalone src/lambda/<name>/index.py sources.

The template embeds the Lambda code inline so it deploys as raw YAML with no
build step; the standalone copies are what we edit and test. This test converts
"keep them in sync by hand" into a mechanism that fails loudly on drift.

To fix a failure: python scripts/sync_lambda_code.py --sync
"""

import os
import sys
import unittest

_SCRIPTS = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts')
sys.path.insert(0, _SCRIPTS)

import sync_lambda_code as sync  # noqa: E402


class TestTemplateInSyncWithSource(unittest.TestCase):
    def test_each_inline_block_matches_its_source(self):
        template = sync._read(sync.TEMPLATE)
        lines = template.splitlines(keepends=True)
        drifted = []
        for logical_id, src_path in sync.LAMBDAS.items():
            start, end, indent = sync._find_block(lines, logical_id)
            inline = sync._inline_text(lines, start, end, indent)
            source = sync._read(src_path)
            if inline != source:
                drifted.append(f"{logical_id} != {os.path.relpath(src_path, sync.REPO)}")
        self.assertEqual(
            drifted, [],
            "Inline Lambda code drifted from source: " + "; ".join(drifted)
            + ". Run: python scripts/sync_lambda_code.py --sync",
        )


class TestZipFileIntegrity(unittest.TestCase):
    """The structural backstop: exactly one `ZipFile: |` block per LAMBDAS entry,
    each under a `Code:` key. Defeats a decoy `ZipFile:` shadowing the real one."""

    def test_current_template_has_exactly_expected_blocks(self):
        lines = sync._read(sync.TEMPLATE).splitlines(keepends=True)
        openers = sync._zipfile_openers(lines)
        self.assertEqual(len(openers), len(sync.LAMBDAS))
        sync.assert_zipfile_integrity(lines)  # must not raise

    def test_decoy_zipfile_under_code_is_counted(self):
        # A decoy Code:/ZipFile: block (e.g. a third would-be Lambda) makes the
        # count exceed len(LAMBDAS) and the guard fails loudly.
        lines = sync._read(sync.TEMPLATE).splitlines(keepends=True)
        decoy = [
            "  DecoyLambda:\n",
            "    Type: AWS::Lambda::Function\n",
            "    Properties:\n",
            "      Code:\n",
            "        ZipFile: |\n",
            "          x = 1\n",
        ]
        with self.assertRaises(sync.SyncError):
            sync.assert_zipfile_integrity(lines + decoy)

    def test_zipfile_literal_inside_embedded_python_not_counted(self):
        # "ZipFile: |" appearing inside embedded code (indented as block content,
        # not under a Code: key) must not be miscounted as a real block.
        lines = sync._read(sync.TEMPLATE).splitlines(keepends=True)
        noise = ["          # a comment mentioning ZipFile: | in passing\n"]
        sync.assert_zipfile_integrity(lines + noise)  # still exactly len(LAMBDAS)


if __name__ == '__main__':
    unittest.main()
