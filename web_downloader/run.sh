#!/bin/bash
cd "$(dirname "$0")/.."
exec python3 web_downloader/app.py
