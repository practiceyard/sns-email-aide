<!-- SPDX-License-Identifier: MIT-0 -->
# Contributing

Thanks for your interest. This is small, focused test-support infrastructure;
changes should keep it that way.

## Setup

```
pip install -r requirements-dev.txt
```

That is boto3 (imported by the Lambda sources) and pytest. The app has no
install-time runtime dependencies of its own: on AWS Lambda, boto3 is provided
by the Python runtime.

## The one thing to know: inline Lambda code

The CloudFormation template (`sns-email-aide.yaml`) embeds each Lambda's code
**inline** so the stack deploys as raw YAML with no build step. The editable,
unit-tested copies live under `src/lambda/<name>/index.py`. These two must stay
byte-identical.

If you change a Lambda:

1. Edit the standalone source under `src/lambda/<name>/index.py`.
2. Copy it into the template's inline block:
   ```
   python scripts/sync_lambda_code.py --sync
   ```
3. Commit both the source and the updated template together.

Run the script with **no argument** to only check for drift (do not modify):
```
python scripts/sync_lambda_code.py
```
It exits non-zero and prints the fix command if the inline code and source
differ. CI runs this check on every push and pull request, so a PR that edits
one copy but not the other will fail until they match.

To have git catch this before you even push, install the optional pre-commit
hook:
```
cp scripts/pre-commit.sample .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

## Tests

```
python -m pytest tests/unit
```

Unit tests need no AWS account (all AWS calls are mocked). Please add or update
tests for behavior changes. The integration test under `tests/integration/`
runs against a live deployment and is not part of the unit suite; see the
[README](README.md#testing-the-setup).

## Pull requests

- Keep changes scoped and the tool minimal - prefer not adding configuration or
  options the use case does not need.
- Make sure `python scripts/sync_lambda_code.py` and `python -m pytest tests/unit`
  both pass locally before opening the PR.
- All source files carry `SPDX-License-Identifier: MIT-0`; contributions are
  under the same license.
