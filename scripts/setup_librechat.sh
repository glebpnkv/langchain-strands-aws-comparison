#!/usr/bin/env bash
set -euo pipefail

git clone https://github.com/danny-avila/LibreChat vendor/LibreChat
cp vendor/LibreChat/.env.example vendor/LibreChat/.env
cp infra/librechat/* vendor/LibreChat/.