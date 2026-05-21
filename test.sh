#!/usr/bin/env bash
set -euo pipefail

python3 -m unittest discover -s tests -p 'test_*.py' -v
